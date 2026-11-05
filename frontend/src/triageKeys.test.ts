import { describe, expect, it } from 'vitest';
import { isActivationFocus, isEditableFocus, moveSelection, resolveTriageKey } from './triageKeys';

describe('resolveTriageKey', () => {
  it('opens on o, defers on d/D only when wired, agents on c only when wired', () => {
    expect(resolveTriageKey('o', { defer: true, agent: true })).toBe('open');
    expect(resolveTriageKey('O', { defer: false, agent: false })).toBe('open');
    expect(resolveTriageKey('d', { defer: true, agent: false })).toBe('defer');
    expect(resolveTriageKey('D', { defer: true, agent: false })).toBe('defer');
    expect(resolveTriageKey('d', { defer: false, agent: false })).toBeNull();
    expect(resolveTriageKey('c', { defer: false, agent: true })).toBe('agent');
    expect(resolveTriageKey('C', { defer: false, agent: true })).toBe('agent');
    expect(resolveTriageKey('c', { defer: false, agent: false })).toBeNull();
  });

  it('ignores other keys', () => {
    expect(resolveTriageKey('x', { defer: true, agent: true })).toBeNull();
    expect(resolveTriageKey('Enter', { defer: true, agent: true })).toBeNull();
    expect(resolveTriageKey('', { defer: true, agent: true })).toBeNull();
  });
});

describe('moveSelection', () => {
  it('returns null for an empty list', () => {
    expect(moveSelection(null, 1, 0)).toBeNull();
    expect(moveSelection(0, -1, 0)).toBeNull();
  });

  it('lands on first/last row from nowhere', () => {
    expect(moveSelection(null, 1, 3)).toBe(0);
    expect(moveSelection(null, -1, 3)).toBe(2);
  });

  it('steps and clamps at the edges', () => {
    expect(moveSelection(0, 1, 3)).toBe(1);
    expect(moveSelection(1, -1, 3)).toBe(0);
    expect(moveSelection(0, -1, 3)).toBe(0);
    expect(moveSelection(2, 1, 3)).toBe(2);
  });
});

describe('triage focus guards', () => {
  it('treats inputs as editable, body as free', () => {
    expect(isEditableFocus({ tagName: 'INPUT' })).toBe(true);
    expect(isEditableFocus({ tagName: 'textarea' })).toBe(true);
    expect(isEditableFocus({ tagName: 'SELECT' })).toBe(true);
    expect(isEditableFocus({ tagName: 'DIV', isContentEditable: true })).toBe(true);
    expect(isEditableFocus({ tagName: 'BODY' })).toBe(false);
    expect(isEditableFocus({ tagName: 'BUTTON' })).toBe(false);
    expect(isEditableFocus(null)).toBe(false);
  });

  it('gives Enter to focused buttons, links, and summaries', () => {
    expect(isActivationFocus({ tagName: 'BUTTON' })).toBe(true);
    expect(isActivationFocus({ tagName: 'A' })).toBe(true);
    expect(isActivationFocus({ tagName: 'SUMMARY' })).toBe(true);
    expect(isActivationFocus({ tagName: 'INPUT' })).toBe(true);
    expect(isActivationFocus({ tagName: 'BODY' })).toBe(false);
    expect(isActivationFocus(null)).toBe(false);
  });
});
