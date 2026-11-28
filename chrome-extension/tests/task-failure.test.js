import { describe, it, expect } from "vitest";
import { describeTaskFailure, redactSecrets } from "../extension/lib/model-download.js";

describe("describeTaskFailure", () => {
  it("adds hint for unavailable/unavailable message", () => {
    const err = new Error("LanguageModel not available");
    const res = describeTaskFailure(err, "Ask");
    expect(res).toContain("Ask failed: LanguageModel not available");
    expect(res).toContain("The on-device model is unavailable. Open the Model section above for status, or chrome://on-device-internals.");
  });

  it("does not add hint for tab error", () => {
    const err = new Error("No active web page found.");
    const res = describeTaskFailure(err, "Suggestion");
    expect(res).toBe("Suggestion failed: No active web page found.");
  });

  it("handles string error", () => {
    const res = describeTaskFailure("Some error", "Ask");
    expect(res).toBe("Ask failed: Some error");
  });

  it("updates undefined error to unknown error", () => {
    const res = describeTaskFailure(undefined, "Suggestion");
    expect(res).toBe("Suggestion failed: unknown error");
  });

  it("handles empty message as unknown error", () => {
    const res = describeTaskFailure("", "Ask");
    expect(res).toBe("Ask failed: unknown error");
  });

  it("does not add hint for generic ReferenceError", () => {
    const err = new ReferenceError("foo is not defined");
    const res = describeTaskFailure(err, "Ask");
    expect(res).toBe("Ask failed: foo is not defined");
  });

  it("adds hint for LanguageModel not defined", () => {
    const err = new Error("LanguageModel is not defined");
    const res = describeTaskFailure(err, "Ask");
    expect(res).toContain("Ask failed: LanguageModel is not defined");
    expect(res).toContain("The on-device model is unavailable. Open the Model section above for status, or chrome://on-device-internals.");
  });
});

describe('redactSecrets and describeTaskFailure redaction', () => {
  it('redacts query param secrets', () => {
    const text = 'Error: key=SECRET123&api_key=SECRET456&apikey=SECRET789&token=FAKE_TOKEN_123&access_token=ACCESS_SECRET&auth=AUTH_SECRET&signature=SIG_SECRET&sig=LAST_SECRET';
    const redacted = redactSecrets(text);
    expect(redacted).not.toContain('SECRET123');
    expect(redacted).not.toContain('SECRET456');
    expect(redacted).not.toContain('SECRET789');
    expect(redacted).not.toContain('FAKE_TOKEN_123');
    expect(redacted).not.toContain('ACCESS_SECRET');
    expect(redacted).not.toContain('AUTH_SECRET');
    expect(redacted).not.toContain('SIG_SECRET');
    expect(redacted).not.toContain('LAST_SECRET');
    expect(redacted).toContain('key=[redacted]');
    expect(redacted).toContain('api_key=[redacted]');
  });

  it('redacts Bearer tokens', () => {
    const text = 'Authorization: Bearer FAKE_TOKEN_123';
    const redacted = redactSecrets(text);
    expect(redacted).not.toContain('FAKE_TOKEN_123');
    expect(redacted).toContain('Bearer [redacted]');
  });

  it('redacts Google API keys', () => {
    const apiKey = 'AIza' + 'x'.repeat(35);
    const text = `Key: ${apiKey}`;
    const redacted = redactSecrets(text);
    expect(redacted).not.toContain(apiKey);
    expect(redacted).toContain('[redacted]');
  });

  it('redacts sk- and ghp_ tokens', () => {
    const text = 'Token sk-FAKE_TOKEN_123 and ghp_FAKE_TOKEN_123';
    const redacted = redactSecrets(text);
    expect(redacted).not.toContain('sk-FAKE_TOKEN_123');
    expect(redacted).not.toContain('ghp_FAKE_TOKEN_123');
    expect(redacted).toContain('[redacted]');
  });

  it('does not alter normal text', () => {
    const text = 'This is a normal error message with no secrets.';
    expect(redactSecrets(text)).toBe(text);
  });

  it('describeTaskFailure redacts error message', () => {
    const err = new Error('Error: key=SECRET123 and token=FAKE_TOKEN_123 and AIza' + 'x'.repeat(35));
    const output = describeTaskFailure(err, 'TestTask');
    expect(output).not.toContain('SECRET123');
    expect(output).not.toContain('FAKE_TOKEN_123');
    const apiKey = 'AIza' + 'x'.repeat(35);
    expect(output).not.toContain(apiKey);
    expect(output).toContain('[redacted]');
  });
});

describe("redactSecrets extra cases", () => {
  it("redacts Basic authorization header", () => {
    expect(redactSecrets('Unauthorized: Basic dXNlcjpwYXNz')).toBe('Unauthorized: Basic [redacted]');
  });

  it("redacts access_token in JSON", () => {
    expect(redactSecrets('{"error":{"message":"bad","access_token":"FAKE_ACCESS_1"}}')).toBe('{"error":{"message":"bad","access_token":"[redacted]"}}');
  });

  it("redacts password query param and preserves other params", () => {
    expect(redactSecrets('https://x.test/login?password=FAKE_PW_1&ok=1')).toBe('https://x.test/login?password=[redacted]&ok=1');
  });

  it("does not redact non-secret param", () => {
    expect(redactSecrets('monkey=banana')).toBe('monkey=banana');
  });

  it("does not redact plain token text", () => {
    expect(redactSecrets('the token is invalid')).toBe('the token is invalid');
  });

  it("redacts client_secret in JSON", () => {
    expect(redactSecrets('{"client_secret":"s3cr3t"}')).toBe('{"client_secret":"[redacted]"}');
  });

  it("redacts password in text style", () => {
    expect(redactSecrets('password: FAKE_PW_2')).toBe('password: [redacted]');
  });

  it("redacts api_key query param", () => {
    expect(redactSecrets('?api_key=abc123')).toBe('?api_key=[redacted]');
  });

  it("preserves existing behavior for key param", () => {
    expect(redactSecrets('key=value')).toBe('key=[redacted]');
  });
});
