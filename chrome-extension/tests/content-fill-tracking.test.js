// Content-script fill registry (#1539/#1540): trackFill signs fields the way
// the side panel looks them up, and the submit handler reports one message per
// fill, only for allowlisted hosts.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fieldSignature } from "../extension/lib/matcher.js";

// Each load() re-imports the content script, which adds document listeners.
// Drop the ones from earlier loads so only the current instance reacts.
const registered = [];
const origAdd = document.addEventListener.bind(document);
document.addEventListener = (...args) => {
  registered.push(args);
  return origAdd(...args);
};

async function load({ allowlist }) {
  for (const args of registered.splice(0)) document.removeEventListener(...args);
  vi.resetModules();
  delete window.__localFormAIInjected;
  window.happyDOM.setURL("https://ok.test/form");
  document.body.innerHTML = `
    <label for="nm">Full name</label><input id="nm" name="nm" type="text">
    <fieldset><legend>Contact preference</legend>
      <label><input type="radio" name="pref" value="email"> By email</label>
      <label><input type="radio" name="pref" value="phone"> By phone</label>
    </fieldset>
    <form id="f"></form>`;
  const sent = [];
  let listener;
  vi.stubGlobal("chrome", {
    storage: { local: { get: async () => ({ observerDomainAllowlist: allowlist }) } },
    runtime: {
      onMessage: { addListener: (fn) => (listener = fn) },
      sendMessage: vi.fn((m) => {
        sent.push(m);
        return Promise.resolve();
      })
    }
  });
  await import("../extension/lib/matcher.legacy.js");
  await import("../extension/content-script.js");
  const hydrate = (doc) =>
    new Promise((resolve) => listener({ action: "HYDRATE_PAGE_FORMS", document: doc, options: { trackFill: true, fillId: "fill-1" } }, {}, resolve));
  const submit = async () => {
    document.getElementById("f").dispatchEvent(new window.Event("submit", { bubbles: true }));
    await new Promise((r) => setTimeout(r, 10));
  };
  return { sent, hydrate, submit };
}

const doc = {
  fields: [
    { fieldId: "a", query: "Full name", value: "Ada Lovelace", inputType: "text", locator: { id: "nm", name: "nm" }, pass: "alias", entryKey: "Full Name", entryId: "basics.full_name", confidence: 0.98 },
    {
      fieldId: "b",
      query: "Contact preference",
      value: "email",
      inputType: "radio-group",
      locator: { name: "pref" },
      constraints: {},
      pass: "alias",
      entryKey: "Contact Preference",
      entryId: "contact.pref",
      confidence: 0.98
    }
  ]
};

describe("content-script fill tracking", () => {
  beforeEach(() => vi.unstubAllGlobals());

  it("signs a radio group like the side panel looks it up and reports once on submit", async () => {
    const { sent, hydrate, submit } = await load({ allowlist: ["ok.test"] });
    const res = await hydrate(doc);
    expect(res.filledCount).toBe(2);
    document.getElementById("nm").value = "Ada L.";
    await submit();
    const msgs = sent.filter((m) => m.type === "learning:recordOutcomes");
    expect(msgs).toHaveLength(1);
    expect(msgs[0]).toMatchObject({ fillId: "fill-1", host: "ok.test" });
    const radio = msgs[0].fields.find((f) => f.entryId === "contact.pref");
    // fieldMatchInput(radio-group field, query) in sidepanel.js
    const lookup = fieldSignature({ label: "contact preference", autocomplete: undefined, name: "pref", type: "radio" });
    expect(fieldSignature(radio.sig)).toBe(lookup);
    expect(radio.submitted).toBe("email");
    expect(msgs[0].fields.find((f) => f.entryId === "basics.full_name").submitted).toBe("Ada L.");
    await submit();
    expect(sent.filter((m) => m.type === "learning:recordOutcomes")).toHaveLength(1);
  });

  it("reports nothing when the host is not on the observer allowlist", async () => {
    const { sent, hydrate, submit } = await load({ allowlist: [] });
    await hydrate(doc);
    await submit();
    expect(sent.filter((m) => m.type === "learning:recordOutcomes")).toHaveLength(0);
  });
});

describe("content-script observer on SPA step and final buttons", () => {
  beforeEach(() => vi.unstubAllGlobals());

  async function loadWithButtons(allowlist, extra = "") {
    const ctx = await load({ allowlist });
    document.body.insertAdjacentHTML(
      "beforeend",
      `<div role="dialog">${extra}<button type="button" id="next-btn">Next</button><button type="button" id="submit-btn">Submit application</button><button type="button" id="save-btn">Save</button><button type="button" id="cancel-btn">Dismiss</button></div>`
    );
    document.getElementById("nm").value = "Ada";
    return ctx;
  }
  const click = async (id, wait = 600) => {
    document.getElementById(id).dispatchEvent(new window.Event("click", { bubbles: true }));
    await new Promise((r) => setTimeout(r, wait));
  };
  const obs = (sent) => sent.filter((m) => m.action === "FORM_SUBMIT_DETECTED");

  it("step navigation is recorded as a step, never as a submit", async () => {
    const { sent } = await loadWithButtons(["ok.test"]);
    await click("next-btn");
    expect(obs(sent)).toHaveLength(1);
    expect(obs(sent)[0].payload.observationKind).toBe("step");
    expect(sent.filter((m) => m.type === "learning:recordOutcomes")).toHaveLength(0);
  });

  it("a genuine final action click is labeled final-click, still not confirmed", async () => {
    const { sent } = await loadWithButtons(["ok.test"]);
    await click("submit-btn");
    expect(obs(sent)[0].payload.observationKind).toBe("final-click");
  });

  it("records nothing on a host that did not opt in", async () => {
    const { sent } = await loadWithButtons([]);
    await click("next-btn");
    expect(obs(sent)).toHaveLength(0);
  });

  it("ignores unrelated Save and Dismiss buttons", async () => {
    const { sent } = await loadWithButtons(["ok.test"]);
    await click("save-btn");
    await click("cancel-btn");
    expect(obs(sent)).toHaveLength(0);
  });

  it("skips a step when the page shows a validation error", async () => {
    const { sent } = await loadWithButtons(["ok.test"], '<input id="bad" aria-invalid="true" value="x">');
    await click("next-btn");
    expect(obs(sent)).toHaveLength(0);
  });

  it("records an identical snapshot once even when click and submit are more than 1.5s apart", async () => {
    const { sent, submit } = await loadWithButtons(["ok.test"]);
    await click("next-btn", 1800);
    await submit();
    await new Promise((r) => setTimeout(r, 50));
    expect(obs(sent)).toHaveLength(1);
  });

  it("a real submit with changed values is recorded and reports outcomes", async () => {
    const { sent, submit } = await loadWithButtons(["ok.test"]);
    await click("next-btn");
    document.getElementById("nm").value = "Ada Lovelace";
    await submit();
    await new Promise((r) => setTimeout(r, 50));
    expect(obs(sent)).toHaveLength(2);
    expect(obs(sent)[1].payload.observationKind).toBe("submit");
  });
});
