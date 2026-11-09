"""Viewer status row + empty state (issue #1203).

The live pill clipped at the viewport edge (flex item kept
min-width:auto, defeating its own ellipsis) and, with no agent
session, the canvas was a bare `-` telemetry readout
indistinguishable from a broken stream. These tests pin the
containment CSS and the explicit empty states against
backend/api/routers/browser_viewer.html (served via
browser_view.VIEWER_HTML).
Run: PYTHONPATH=./deep-agent python3 -m unittest \
    backend.tests.test_viewer_empty_state
"""

import unittest

from backend.api.routers import browser_view

HTML = browser_view.VIEWER_HTML
FLAT = HTML.replace(" ", "")


class PillContainmentTest(unittest.TestCase):
    def test_state_pill_ellipsizes(self):
        # min-width:0 is the actual fix: a flex item defaults to
        # min-width:auto, which refuses to shrink and defeats the
        # overflow/ellipsis the rule already declared.
        self.assertIn("#state{", FLAT)
        rule = FLAT.split("#state{", 1)[1].split("}", 1)[0]
        for prop in (
            "min-width:0",
            "overflow:hidden",
            "text-overflow:ellipsis",
            "white-space:nowrap",
        ):
            self.assertIn(prop, rule, "missing #%s in #state rule" % prop)

    def test_status_bar_boxed_to_viewport(self):
        # The bar must not lay out wider than the viewport (the pill
        # clipped at the viewport edge before max-width:100vw).
        rule = FLAT.split("#bar{", 1)[1].split("}", 1)[0]
        self.assertIn("max-width:100vw", rule)
        self.assertIn("box-sizing:border-box", rule)

    def test_stats_row_contained(self):
        # Long telemetry must ellipsize, never push the row (and the
        # pill above it) out of the viewport.
        rule = FLAT.split("#stats{", 1)[1].split("}", 1)[0]
        for prop in ("overflow:hidden", "text-overflow:ellipsis", "white-space:nowrap"):
            self.assertIn(prop, rule, "missing #%s in #stats rule" % prop)


class EmptyStateTest(unittest.TestCase):
    def test_initial_stats_name_idle_not_bare_dash(self):
        # First paint (before attach) must name the idle state;
        # bare `fps: -` reads as a broken stream.
        self.assertIn("idle \u2014 waiting to attach\u2026", HTML)
        self.assertNotIn('<div id="stats">fps: -', HTML)

    def test_attach_names_kind_aware_waiting_note(self):
        # After attach with no frame yet: shared agent session vs
        # personal view, each waiting for its first frame.
        self.assertIn("shared agent session \u00b7 waiting for first frame\u2026", HTML)
        self.assertIn(
            "personal view (no agent session) \u00b7 waiting for first frame\u2026",
            HTML,
        )

    def test_first_frame_declares_flow(self):
        # The waiting note must clear when frames land (and the 1 Hz
        # readout below takes over flow telemetry).
        self.assertIn("receiving frames\u2026", HTML)

    def test_slot_busy_hint_unchanged(self):
        # The #1205 busy-slot hint (live socket, no frames: single
        # Grid slot held elsewhere) is unchanged by the empty-state
        # rewording and still clears on the first frame.
        self.assertIn("no frames yet", HTML)
        self.assertIn("clearTimeout(noFrameTimer)", HTML)


if __name__ == "__main__":
    unittest.main()
