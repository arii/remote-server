// ============================================================================
// LocalForm AI - "How LocalForm improves" summary (pure view model)
// Reads the state the extension already keeps and returns plain text and links
// for one card at the top of the Observer tab. It adds no learning of its own:
// every line describes something the code does today.
// ============================================================================

import { fillStats } from "./outcomes.js";
import { normalizeGuidance } from "./prompt-guidance.js";
import { normalizeCases } from "./prompt-eval.js";

// Element ids the links point at. tests/improvement-summary.test.js checks each
// one exists in sidepanel.html.
export const LINK_TARGETS = {
  vault: { tab: "tab-vault", el: "vault-count-badge" },
  pending: { tab: "tab-observer", el: "observer-queue-count" },
  mappings: { tab: "tab-observer", el: "learned-container" },
  observe: { tab: "tab-observer", el: "btn-observe-toggle" },
  guidance: { tab: "tab-ai", el: "prompt-tuning", open: true },
  suggest: { tab: "tab-ai", el: "btn-guidance-suggest", open: true },
  samples: { tab: "tab-ai", el: "btn-eval-run", open: true }
};

export const LOOP = [
  {
    kind: "auto",
    text: "On sites where Observer is on, after a fill LocalForm records per field whether you kept, edited or cleared it (the class only, never the value)."
  },
  {
    kind: "auto",
    text: "When you change a field to a different vault entry it learns a field mapping. A new mapping is never used to fill until you confirm it or 3 submissions agree."
  },
  { kind: "auto", text: "The Observer queues facts it sees you type, as suggestions only." },
  {
    kind: "you",
    text: "You approve suggested facts, confirm or delete mappings, accept prompt guidance suggestions, and save and run samples. Nothing changes your vault or prompts without your accept."
  }
];

const count = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
const list = (v) => (Array.isArray(v) ? v : []);

function guidanceLines(g) {
  const perPrompt = Object.keys(g.perPrompt).length;
  const global = g.global ? 1 : 0;
  const last = g.history[g.history.length - 1];
  return { global, perPrompt, total: global + perPrompt, lastSource: last?.source === "suggestion" ? "an accepted suggestion" : last ? "you" : "" };
}

export function improvementSummary({
  vault,
  pendingDeltas,
  learnedMappings,
  learnedAliases,
  fillOutcomes,
  promptGuidance,
  promptEvalCases,
  promptEvalRuns
} = {}) {
  const facts = list(vault).length;
  const pending = list(pendingDeltas).length;
  const mappings = list(learnedMappings);
  const confirmed = mappings.filter((m) => m?.status === "confirmed").length;
  const unconfirmed = mappings.length - confirmed;
  const aliases = list(learnedAliases).length;
  const stats = fillStats(fillOutcomes);
  const guidance = guidanceLines(normalizeGuidance(promptGuidance));
  const cases = normalizeCases(promptEvalCases);
  const runs = list(promptEvalRuns);
  const lastRun = runs[runs.length - 1];

  const items = [
    { id: "vault", text: `${count(facts, "fact")} in your vault.`, link: LINK_TARGETS.vault },
    {
      id: "mappings",
      text: `${count(confirmed, "confirmed field mapping")}, ${unconfirmed} waiting for you to confirm. ${count(aliases, "learned alias", "learned aliases")}.`,
      link: LINK_TARGETS.mappings
    },
    {
      id: "fills",
      text: stats.fills
        ? `${count(stats.fills, "fill")} recorded. Last fill: ${stats.last.kept} kept, ${stats.last.edited} edited, ${stats.last.cleared} cleared. ${Math.round(stats.keptRate * 100)}% of fields kept over recent fills.`
        : "No fills recorded yet.",
      link: LINK_TARGETS.observe
    },
    {
      id: "pending",
      text: pending ? `${count(pending, "suggested fact")} waiting for your review.` : "No suggested facts waiting.",
      link: LINK_TARGETS.pending
    },
    {
      id: "guidance",
      text: guidance.total
        ? `${count(guidance.total, "guidance line")} active (${guidance.global} global, ${guidance.perPrompt} per prompt). Last edit by ${guidance.lastSource || "you"}.`
        : "No prompt guidance set.",
      link: LINK_TARGETS.guidance
    },
    {
      id: "samples",
      text: lastRun
        ? `Last sample run: ${lastRun.passed}/${lastRun.total} passed, score ${Math.round(lastRun.score * 100)}%.`
        : cases.length
          ? `${count(cases.length, "sample")} saved, not run yet.`
          : "No samples saved.",
      link: LINK_TARGETS.samples
    }
  ];

  let next;
  if (pending) next = { text: `Review the ${count(pending, "suggested fact")} in the Review & Merge Queue.`, link: LINK_TARGETS.pending };
  else if (unconfirmed) next = { text: `Confirm or delete the ${count(unconfirmed, "field mapping")} waiting for you.`, link: LINK_TARGETS.mappings };
  else if (!stats.fills) next = { text: "Turn on Observer on a site and submit a form so your edits are recorded.", link: LINK_TARGETS.observe };
  else if (!guidance.total) next = { text: "Press Suggest improvements to get guidance ideas from your recorded fills.", link: LINK_TARGETS.suggest };
  else if (cases.length) next = { text: "Run samples to check the effect of your guidance.", link: LINK_TARGETS.samples };
  else next = { text: "Nothing waiting.", link: LINK_TARGETS.observe };

  return { loop: LOOP, items, next };
}
