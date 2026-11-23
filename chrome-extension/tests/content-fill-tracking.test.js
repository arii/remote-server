// Content-script fill registry (#1539/#1540): trackFill signs fields the way
// the side panel looks them up, and the submit handler reports one message per
// fill, only for allowlisted hosts.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fieldSignature } from "../extension/lib/matcher.js";

async function load({ allowlist }) {
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
