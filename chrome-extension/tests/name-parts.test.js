import { describe, expect, it } from "vitest";
import { deriveNamePart, namePartKind } from "../extension/lib/name-parts.js";
import { matchField } from "../extension/lib/matcher.js";

const vault = [{ id: "identity.full_name", key: "Full Name", value: "Ariel Anders", aliases: [] }];
describe("name parts", () => {
  it("shows the legal-name labels do not match a full-name entry directly", () => {
    expect(matchField({ label: "legal first name", type: "text" }, vault)).toBeNull();
    expect(matchField({ label: "legal last name", type: "text" }, vault)).toBeNull();
  });
  it("derives first and last from a two-word full name", () => {
    expect(deriveNamePart("Legal first name *", vault).value).toBe("Ariel");
    expect(deriveNamePart("Legal last name", vault).value).toBe("Anders");
    expect(deriveNamePart("Family name", vault).value).toBe("Anders");
  });
  it("does not guess for ambiguous or missing names", () => {
    expect(deriveNamePart("First name", [{ key: "Full Name", value: "Mary Jane Watson" }])).toBeNull();
    expect(deriveNamePart("First name", [{ key: "Email", value: "a@b.co" }])).toBeNull();
    expect(deriveNamePart("Company name", vault)).toBeNull();
    expect(namePartKind("Preferred first name")).toBeNull();
  });
});
