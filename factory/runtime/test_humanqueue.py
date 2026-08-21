#!/usr/bin/env python3
"""Tests for the human-queue pass.

Run: python3 -m unittest discover -s factory/runtime -p 'test_*.py' -v

The property under test is unusual: it is about **repetition**. Most of this
repository tests that something happens exactly once. Here the requirement is
the opposite — an artifact that is still waiting must be announced again, and
again, for as long as it waits. A notifier that remembers what it has already
said goes quiet about a problem precisely because the problem is old, and that
is the failure this pass exists to prevent (#55, #61 and #66 each sat at a bell
until somebody happened to look).
"""

from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stdout
from unittest import mock

import humanqueue as hq  # noqa: E402 — inserts the dispatcher path
import dispatcher  # noqa: E402


def labels(*names):
    return [{"name": n} for n in names]


def issue(number, *names, assoc="OWNER", title="t", state="open"):
    return {"number": number, "state": state, "author_association": assoc,
            "title": title, "html_url": f"https://github.com/o/r/issues/{number}",
            "labels": labels(*names)}


class TestWhatCounts(unittest.TestCase):
    def test_every_state_section_9_11_names_is_waiting(self):
        cases = {
            "project:awaiting-ready": issue(1, "type:project", "project:awaiting-ready"),
            "project:awaiting-acceptance": issue(2, "type:project", "project:awaiting-acceptance"),
            "story:blocked:poison": issue(3, "type:story", "story:blocked:poison"),
            "story:blocked:scope": issue(4, "type:story", "story:blocked:scope"),
        }
        for state, artifact in cases.items():
            item = hq.waiting_for(artifact)
            self.assertIsNotNone(item, state)
            self.assertEqual(item.state, state)
            self.assertTrue(item.action.strip(), state)

    def test_the_set_is_exactly_section_9_11s_last_row(self):
        """A state added here without being added to the contract is a queue
        entry no reader can check against the spec."""
        self.assertEqual(
            set(hq.WAITING_ON_A_HUMAN),
            {"project:awaiting-ready", "project:awaiting-acceptance",
             dispatcher.POISON, "story:blocked:scope"})

    def test_working_states_are_not_waiting(self):
        for state in ("story:ready", "story:claimed", "story:in-review",
                      "story:merged", "story:completed", "story:cancelled",
                      "story:blocked", "project:active", "project:accepted",
                      "project:queued", "project:planning"):
            kind = "type:project" if state.startswith("project:") else "type:story"
            self.assertIsNone(hq.waiting_for(issue(9, kind, state)), state)

    def test_an_artifact_with_no_lifecycle_label_is_not_waiting(self):
        self.assertIsNone(hq.waiting_for(issue(9, "type:story")))

    def test_ambiguous_lifecycle_labels_are_not_assigned_a_state(self):
        """Two labels is ambiguous, not "the first" — the same rule the
        dispatcher applies, and for the same reason."""
        ambiguous = issue(9, "type:story", "story:blocked:poison", "story:ready")
        self.assertIsNone(hq.waiting_for(ambiguous))

    def test_every_action_names_the_exact_form_the_parser_will_read(self):
        """#122 — the defect this fixes, pinned against the real parser.

        Not a string comparison against a copy of the instruction: that would
        pass forever while the parser moved underneath it. Each promised literal
        is fed to `continuation.py`'s own regex, so an instruction and its
        consumer cannot drift apart in silence — drift is the failure mode, and
        the reason a fixed expected-string test would not prevent a recurrence.
        """
        import continuation

        for state, promised in hq.HUMAN_QUEUE_FORMATS.items():
            if promised is None:
                continue
            literal, heading = promised
            action = hq.WAITING_ON_A_HUMAN[state]
            self.assertIn(literal, action,
                          f"{state}: the action does not name the line it needs")
            self.assertIn(heading, action,
                          f"{state}: the action does not name the required heading")

            # The literal must actually satisfy the pattern that will read it.
            pattern = (continuation.DECISION_RE if literal.startswith("decision:")
                       else continuation.RESULT_RE)
            self.assertIsNotNone(pattern.search(literal),
                                 f"{state}: `{literal}` does not match the parser "
                                 f"that will consume the reply")

            # And the heading must be the one the consumer matches exactly.
            heading_re = (continuation.APPROVAL_HEADING if "approval" in heading.lower()
                          else continuation.ACCEPTANCE_HEADING)
            self.assertIsNotNone(heading_re.search(heading),
                                 f"{state}: `{heading}` is not the heading "
                                 f"continuation.py recognises")

    def test_an_action_reply_built_from_the_instruction_is_actually_consumed(self):
        """End to end: follow the instruction literally, and it must work.

        The #109 failure was that a person did what the queue asked and the
        factory still refused it. This builds the minimal comment the action
        describes and pushes it through the real classifier.
        """
        import continuation

        approval = ("## Plan approval\n\ndecision: approved\nactor: @someone\n")
        verdict, error = continuation.classify_approval({"body": approval})
        self.assertIsNone(error, approval)
        self.assertEqual(verdict, "approved")

        acceptance = ("## Acceptance\n\nresult: pass\nactor: @someone\n\n"
                      "- it works — pass\n")
        verdict, error = continuation.classify_acceptance({"body": acceptance})
        self.assertIsNone(error, acceptance)
        self.assertEqual(verdict, "pass")

    def test_the_109_comment_would_still_be_refused(self):
        """The prose that started this is still not a decision.

        The fix is the instruction, never the parser. If this test ever fails,
        someone has taught the factory to read free text to decide whether it is
        authorized, which is the one thing §9.9 forbids outright.
        """
        import continuation

        _, error = continuation.classify_approval(
            {"body": "## Plan approval\n\nAPPROVED.\n\nProceed with the work.\n"})
        self.assertEqual(error, continuation.Reason.MALFORMED_DECISION)

    def test_a_state_with_no_automated_consumer_says_so(self):
        """An invented format for a decision nothing reads would be worse than
        none: it implies a machine is waiting when no machine is."""
        for state, promised in hq.HUMAN_QUEUE_FORMATS.items():
            if promised is not None:
                continue
            action = hq.WAITING_ON_A_HUMAN[state]
            self.assertNotIn("decision:", action, state)
            self.assertNotIn("result:", action, state)

    def test_every_waiting_state_declares_whether_it_has_a_consumer(self):
        self.assertEqual(set(hq.HUMAN_QUEUE_FORMATS), set(hq.WAITING_ON_A_HUMAN),
                         "a waiting state with no declared format is a state "
                         "nobody checked for drift")

    def test_the_poison_action_names_all_three_rescue_components(self):
        """§4.3.6 requires all three, in order. Naming one is how a rescue gets
        half-performed and the story sits poisoned anyway."""
        action = hq.WAITING_ON_A_HUMAN[dispatcher.POISON]
        for component in ("rescue comment", "`0`", "poison-rescue"):
            self.assertIn(component, action, component)

    def test_the_poison_label_comes_from_the_authority(self):
        with open(hq.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("dispatcher.POISON", source)
        self.assertNotIn('"story:blocked:poison"', source)


class TestTheTrustBoundaryRunsTheOtherWay(unittest.TestCase):
    """§9.9: untrusted text may be *read* — "for context, for a human's queue".
    A queue entry authorizes nothing, so an untrusted artifact is listed and
    marked rather than hidden. Hiding it would be the worse error."""

    def test_an_untrusted_artifact_is_still_listed(self):
        item = hq.waiting_for(issue(9, "type:story", "story:blocked:scope", assoc="NONE"))
        self.assertIsNotNone(item)
        self.assertFalse(item.trusted)

    def test_it_is_marked_so_nobody_reads_it_as_factory_state(self):
        item = hq.waiting_for(issue(9, "type:story", "story:blocked:scope", assoc="NONE"))
        self.assertIn("trust=untrusted", item.line())

    def test_a_trusted_artifact_carries_no_marker(self):
        item = hq.waiting_for(issue(9, "type:story", "story:blocked:scope"))
        self.assertNotIn("trust=", item.line())


class TestTheLine(unittest.TestCase):
    def item(self, **kw):
        return hq.waiting_for(issue(kw.pop("number", 42), "type:story",
                                    "story:blocked:poison", **kw))

    def test_it_carries_identity_state_link_and_action(self):
        line = self.item().line()
        self.assertIn("artifact=#42", line)
        self.assertIn("state=story:blocked:poison", line)
        self.assertIn("url=https://github.com/o/r/issues/42", line)
        self.assertIn("action=", line)

    def test_the_action_names_what_the_human_must_do(self):
        self.assertIn("§4.3.6", self.item().line())

    def test_it_carries_no_business_context(self):
        """Like the dispatcher's DISPATCH line: the moment a queue item copies
        business context, the relay has been rebuilt as infrastructure."""
        line = self.item(title="Implement the widget exporter for Q3").line()
        self.assertNotIn("widget", line)
        self.assertNotIn("Spec", line)

    def test_it_is_one_line_even_for_a_multi_line_action(self):
        for item in hq.waiting(
                {n: issue(n, "type:story", "story:blocked:scope") for n in (1, 2)}):
            self.assertEqual(len(item.line().splitlines()), 1)


class TestThePass(unittest.TestCase):
    def run_pass(self, issues):
        out = io.StringIO()
        with mock.patch.object(dispatcher, "fetch_issues", return_value=issues), \
             mock.patch.object(hq.runlog, "event") as event, \
             redirect_stdout(out):
            items = hq.run("o/r", "token")
        return items, out.getvalue(), event

    def waiting_repo(self):
        return {5: issue(5, "type:project", "project:awaiting-ready"),
                6: issue(6, "type:story", "story:blocked:poison"),
                7: issue(7, "type:story", "story:ready")}

    def test_it_lists_everything_waiting_and_nothing_else(self):
        items, output, _ = self.run_pass(self.waiting_repo())
        self.assertEqual([i.number for i in items], [5, 6])
        self.assertIn("2 artifact(s) waiting", output)
        self.assertNotIn("artifact=#7", output)

    def test_an_empty_queue_still_says_so(self):
        """"Nothing is waiting" is a fact worth stating once per poll: its
        absence is indistinguishable from the pass not having run."""
        _, output, _ = self.run_pass({7: issue(7, "type:story", "story:ready")})
        self.assertIn("nothing is waiting on a human", output)

    def test_a_still_waiting_artifact_is_announced_again_on_the_next_pass(self):
        """The property this module exists for. Nothing records that an artifact
        was announced, so nothing can decide it has been announced enough."""
        repo = self.waiting_repo()
        first, output_one, _ = self.run_pass(repo)
        second, output_two, _ = self.run_pass(repo)
        self.assertEqual([i.number for i in first], [i.number for i in second])
        self.assertEqual(output_one, output_two)

    def test_an_artifact_that_stops_waiting_stops_being_announced(self):
        repo = self.waiting_repo()
        _, before, _ = self.run_pass(repo)
        repo[6] = issue(6, "type:story", "story:ready")
        _, after, _ = self.run_pass(repo)
        self.assertIn("artifact=#6", before)
        self.assertNotIn("artifact=#6", after)

    def test_each_waiting_artifact_produces_a_structured_event(self):
        _, _, event = self.run_pass(self.waiting_repo())
        names = [call.args[0] for call in event.call_args_list]
        self.assertEqual(names.count("human_queue.waiting"), 2)
        self.assertEqual(names.count("human_queue.summary"), 1)

    def test_the_event_carries_the_link_and_the_action(self):
        _, _, event = self.run_pass(self.waiting_repo())
        first = next(c for c in event.call_args_list if c.args[0] == "human_queue.waiting")
        self.assertIn("url", first.kwargs)
        self.assertIn("action", first.kwargs)
        self.assertIn("state", first.kwargs)

    def test_ordering_is_deterministic(self):
        items, _, _ = self.run_pass({
            9: issue(9, "type:story", "story:blocked:scope"),
            2: issue(2, "type:project", "project:awaiting-ready"),
            5: issue(5, "type:story", "story:blocked:poison")})
        self.assertEqual([i.number for i in items], [2, 5, 9])


class TestItNeverWrites(unittest.TestCase):
    """An artifact waiting on a human is waiting *because* no component may
    decide it. A notifier that could change what it reports on would not be a
    notifier."""

    def test_the_pass_issues_no_write(self):
        seen = []

        def recording(url, token, method="GET", payload=None):
            seen.append(method)
            return []

        with mock.patch.object(dispatcher, "_api", recording), \
             redirect_stdout(io.StringIO()):
            hq.run("o/r", "token")
        self.assertTrue(seen)
        self.assertEqual(set(seen), {"GET"})

    def test_the_module_names_no_write_method(self):
        with open(hq.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ('"PATCH"', '"POST"', '"PUT"', '"DELETE"'):
            self.assertNotIn(forbidden, source)

    def test_the_module_persists_no_local_state(self):
        with open(hq.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for forbidden in ("open(", "json.dump", "seen", "cursor", "cache"):
            self.assertNotIn(forbidden, source.split("PREFIXES")[1],
                             f"{forbidden} would let the pass decide it has said enough")


class TestThePoisonEntryDescribesTheRuleThatExists(unittest.TestCase):
    """#181 — pin the poison action against `poison_closure`, not against itself.

    The entry used to end *"or leave it and let §4.3.7 close it as not planned"*.
    A fixed-string assertion would have passed against that text, because the
    string matched itself; what made it wrong was a claim about a component's
    future behaviour, and the component is right here to be asked. So these tests
    drive `dispatcher.poison_closure` and require the prose to agree with it.
    """

    def action(self) -> str:
        return hq.WAITING_ON_A_HUMAN[dispatcher.POISON]

    def test_inaction_never_closes_it_however_many_polls_pass(self):
        """The claim the old text made, asked of the component that would do it."""
        timeline = []                      # nobody rescues, nobody cancels
        for _ in range(dispatcher.RESCUE_MAX + 5):
            closes, _ = dispatcher.poison_closure(timeline)
            self.assertFalse(closes,
                             "nothing closes a poisoned story that no one acts on; "
                             "the timeline only grows when a human does something")
        self.assertIn("stays open", self.action(),
                      "poison_closure says the story stays open, so the entry must "
                      "say so too rather than implying the wait ends by itself")

    def test_every_mention_of_the_closure_rule_carries_the_cap_that_gates_it(self):
        # The original defect in one line: the entry cited §4.3.7 and omitted the
        # cap, turning a conditional rule into an unconditional promise. Closure
        # may still be named — cancellation closes immediately and correctly, by a
        # human — but §4.3.7 may never be invoked without what gates it.
        for clause in re.split(r"(?<=[.;])\s+", self.action()):
            if "§4.3.7" in clause:
                self.assertIn(str(dispatcher.RESCUE_MAX), clause,
                              "§4.3.7 is stated without its rescue cap, which is "
                              "how it was read as 'leave it and it closes'")

    def test_closure_arrives_only_once_the_cap_is_spent(self):
        poisoning = {"event": "labeled", "label": {"name": dispatcher.POISON}}
        for already in range(dispatcher.RESCUE_MAX):
            closes, _ = dispatcher.poison_closure([poisoning] * already)
            self.assertFalse(closes, f"poisoning {already + 1} must not close")
        closes, _ = dispatcher.poison_closure([poisoning] * dispatcher.RESCUE_MAX)
        self.assertTrue(closes, "the cap being spent is what closes it")

    def test_both_dispositions_9_4_1_offers_are_named(self):
        action = self.action()
        self.assertIn("§4.3.6", action, "the rescue route must be named")
        self.assertIn(dispatcher.CANCELLED, action,
                      "§9.4.1 offers cancellation and the queue never mentioned it, "
                      "so the one route an abandoned story needs was unreachable "
                      "from the only place that asks about it")

    def test_the_rescue_still_names_all_three_parts(self):
        action = self.action()
        for part in ("rescue comment", "`### Attempt` reset to `0`", "poison-rescue"):
            self.assertIn(part, action, f"§4.3.6 requires {part}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
