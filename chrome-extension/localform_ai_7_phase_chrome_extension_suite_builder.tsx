import React, { useState, useEffect, useMemo } from 'react';
import {
  Cpu,
  Download,
  Upload,
  Sparkles,
  Database,
  Wand2,
  Eye,
  CheckCircle2,
  AlertTriangle,
  Code2,
  Play,
  FileJson,
  Copy,
  Check,
  Trash2,
  RefreshCw,
  Search,
  Layers,
  Terminal,
  Sliders,
  FolderArchive,
  Globe,
  FileText,
  ChevronRight
} from 'lucide-react';

// ============================================================================
// 1. DEFAULT USER CONTEXT VAULT DATA (PHASE 5 SEED — ARIEL ANDERS PROFILE)
// ============================================================================
const INITIAL_CONTEXT_ENTRIES = [
  {
    id: 'identity.full_name',
    category: 'identity',
    key: 'Legal Full Name',
    aliases: ['full name', 'name', 'your name', 'applicant name', 'speaker name', 'first and last name'],
    value: 'Ariel Anders',
    tags: ['personal', 'identity', 'core'],
    autocomplete: 'name',
    metadata: { updatedAt: '2026-11-10T01:42:00Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'contact.email',
    category: 'contact',
    key: 'Primary Email',
    aliases: ['email', 'email address', 'work email', 'contact email', 'speaker email'],
    value: 'anders.ariel@gmail.com',
    tags: ['contact', 'work', 'core'],
    autocomplete: 'email',
    metadata: { updatedAt: '2026-11-10T01:27:41.632Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'contact.phone',
    category: 'contact',
    key: 'Phone Number',
    aliases: ['phone', 'phone number', 'mobile', 'telephone', 'cell phone', 'contact number', 'direct phone'],
    value: '+1 (661) 205-2489',
    tags: ['contact', 'core'],
    autocomplete: 'tel',
    metadata: { updatedAt: '2026-11-10T01:26:34.938Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'location.current_base',
    category: 'location',
    key: 'Current Location / Metro Area',
    aliases: ['where are you currently based', 'current location', 'location', 'based in', 'city and state', 'start typing...'],
    value: 'San Francisco, CA',
    tags: ['location', 'core', 'ashby'],
    autocomplete: '',
    metadata: { updatedAt: '2026-11-10T01:42:00Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'professional.role',
    category: 'professional',
    key: 'Current Role / Title',
    aliases: ['job title', 'role', 'position', 'occupation', 'current title', 'title'],
    value: 'Consultant',
    tags: ['work', 'career'],
    autocomplete: 'organization-title',
    metadata: { updatedAt: '2026-11-10T01:26:43.272Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'professional.company',
    category: 'professional',
    key: 'Company / Organization',
    aliases: ['company', 'organization', 'employer', 'workplace', 'company name', 'research lab'],
    value: 'Ariel Anders Consulting',
    tags: ['work', 'career'],
    autocomplete: 'organization',
    metadata: { updatedAt: '2026-11-10T01:26:47.798Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'professional.previous_companies',
    category: 'professional',
    key: 'Prior Robotics Experience (Waymo & Robust.AI)',
    aliases: ['previous company', 'past employers', 'work history', 'prior experience'],
    value: 'Waymo (Senior Software Engineer, Motion Planning for Pickups/Drop-offs); Robust.AI (First Roboticist hired, architected reactive social navigation stack using Behavior Trees)',
    tags: ['work', 'career', 'robotics'],
    autocomplete: '',
    metadata: { updatedAt: '2026-11-10T01:42:00Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'education.phd',
    category: 'professional',
    key: 'Education (MIT PhD & UC Santa Cruz BS)',
    aliases: ['education', 'degree', 'phd', 'doctorate', 'university', 'alma mater'],
    value: 'Ph.D. in Computer Science (Robotics), MIT; B.S. in Computer Engineering, UC Santa Cruz',
    tags: ['education', 'mit', 'robotics'],
    autocomplete: '',
    metadata: { updatedAt: '2026-11-10T01:42:00Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'location.address',
    category: 'location',
    key: 'Street Address',
    aliases: ['address', 'street address', 'mailing address', 'shipping address'],
    value: '1 Brady St Apt 123',
    tags: ['location', 'shipping'],
    autocomplete: 'street-address',
    metadata: { updatedAt: '2026-11-10T01:26:57.126Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'location.city',
    category: 'location',
    key: 'City',
    aliases: ['city', 'town', 'locality'],
    value: 'San Francisco',
    tags: ['location'],
    autocomplete: 'address-level2',
    metadata: { updatedAt: '2026-11-10T01:22:54.176Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'social.portfolio',
    category: 'social',
    key: 'Portfolio Website URL',
    aliases: ['portfolio', 'website', 'personal website', 'portfolio url', 'personal projects', 'work samples'],
    value: 'https://arii.github.io',
    tags: ['social', 'work', 'links', 'core'],
    autocomplete: 'url',
    metadata: { updatedAt: '2026-11-10T05:00:00Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'social.github',
    category: 'social',
    key: 'GitHub Profile URL',
    aliases: ['github', 'github url', 'code repo', 'github profile', 'repositories'],
    value: 'https://github.com/arii',
    tags: ['social', 'work', 'links', 'core'],
    autocomplete: 'url',
    metadata: { updatedAt: '2026-11-10T05:00:00Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'social.linkedin',
    category: 'social',
    key: 'LinkedIn Profile URL',
    aliases: ['linkedin', 'linkedin url', 'linkedin profile'],
    value: 'https://www.linkedin.com/in/ariel-anders',
    tags: ['social', 'work', 'links', 'core'],
    autocomplete: 'url',
    metadata: { updatedAt: '2026-11-10T05:00:00Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'social.personal_projects',
    category: 'social',
    key: 'Personal Projects & Portfolio URL',
    aliases: ['personal projects', 'projects', 'portfolio', 'website', 'portfolio url', 'personal website'],
    value: 'https://arii.github.io',
    tags: ['social', 'work', 'links', 'ashby'],
    autocomplete: 'url',
    metadata: { updatedAt: '2026-11-10T05:00:00Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'preferences.visa_sponsorship',
    category: 'preferences',
    key: 'Require Visa Sponsorship',
    aliases: [
      'will you now or in the future require sponsorship',
      'require sponsorship for employment visa',
      'require sponsorship',
      'visa sponsorship',
      'require sponsorship for employment visa status',
      'sponsorship'
    ],
    value: 'No',
    tags: ['career', 'ashby', 'compliance'],
    autocomplete: '',
    metadata: { updatedAt: '2026-11-10T01:42:00Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'preferences.onsite_commitment',
    category: 'preferences',
    key: 'Onsite San Francisco Commitment',
    aliases: [
      'full-time onsite role based out of our office in san francisco',
      'are you able to commit to this requirement',
      'commit to this requirement',
      'onsite role',
      'are you willing to relocate to san francisco',
      'if you already live here, please click yes',
      'willing to relocate',
      'relocate to san francisco'
    ],
    value: 'Yes',
    tags: ['career', 'preferences', 'ashby'],
    autocomplete: '',
    metadata: { updatedAt: '2026-11-10T01:42:00Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'custom.coolest_project',
    category: 'custom',
    key: "Share something cool you've built",
    aliases: [
      "share something cool you've built",
      'share something cool',
      "something you've built",
      'coolest project',
      'describe a project',
      'what failed and what you changed'
    ],
    value: "Autonomous Forklift Localization & Tilt-Compensated Sensor Fusion (Civ Robotics): Built and deployed core C++, Python, and ROS 2 navigation and localization pipelines on physical autonomous forklifts operating in rugged outdoor field sites. When we hit critical localization drift on uneven terrain, I led root-cause analysis across the hardware/software boundary—engineering custom IMU + GPS sensor-filtering drivers and real-time tilt-compensation logic that stabilized pose estimation and significantly increased fleet uptime.\n\nReal-Time Web Bluetooth & Multi-Client Telemetry Pipeline (https://github.com/arii/hrm): Built an open-source, low-latency biometric telemetry platform streaming live BLE heart-rate sensor packets over WebSockets to multi-client dashboards, paired with an autonomous phone-first operator console (remote-server) for managing local coding and robotics workflows.",
    tags: ['projects', 'robotics', 'c++', 'ros2'],
    autocomplete: '',
    metadata: { updatedAt: '2026-11-10T05:00:00Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'custom.bio',
    category: 'custom',
    key: 'Professional Bio & Summary',
    aliases: ['bio', 'about you', 'summary', 'why should we pick you', 'project description', 'speaker bio', 'qualifications'],
    value: "I'm an MIT PhD roboticist. My work centers on robot system architecture, reliability, and handling uncertainty: getting robots to behave in messy real-world conditions. Previously, I was a Senior Software Engineer on the Planning team at Waymo, developing onboard motion planning and decision-making software, including pickups and drop-offs. Before that, I was the first roboticist hired at Robust.AI, an early-stage startup, where I architected the reactive social navigation stack using behavior trees. I received a BS in Computer Engineering from UC Santa Cruz and my Doctorate in Computer Science from MIT, where my research focused on reliable robotic manipulation under uncertainty, with the vision of enabling household helpers. At MIT I also taught project-based collaborative robotics courses and built an iOS app for people with visual impairments. I'm currently considering full-time roles and consulting/project-based work in the Bay Area.",
    tags: ['bio', 'conference', 'grant'],
    autocomplete: '',
    metadata: { updatedAt: '2026-11-10T01:27:32.364Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'preferences.dietary',
    category: 'preferences',
    key: 'Dietary Restrictions',
    aliases: ['dietary', 'food allergies', 'meal preference', 'dietary restrictions', 'dietary notes'],
    value: 'Dairy free',
    tags: ['events', 'preferences'],
    autocomplete: '',
    metadata: { updatedAt: '2026-11-10T01:27:07.345Z', source: 'manual', confidence: 1.0 }
  },
  {
    id: 'preferences.tshirt',
    category: 'preferences',
    key: 'Apparel / T-Shirt Size',
    aliases: ['t-shirt size', 'shirt size', 'swag size', 'apparel', 'hoodie size'],
    value: 'Unisex L',
    tags: ['events', 'preferences'],
    autocomplete: '',
    metadata: { updatedAt: '2026-11-10T01:22:54.176Z', source: 'manual', confidence: 1.0 }
  }
];

// ============================================================================
// 2. COMPLETE EXTENSION SOURCE CODE GENERATOR (MANIFEST V3 + ALL 7 PHASES)
// ============================================================================
const EXTENSION_FILES = {
  'manifest.json': `{
  "manifest_version": 3,
  "name": "LocalForm AI — 7-Phase Built-in AI Form & Context Suite",
  "version": "1.1.0",
  "description": "On-device Chrome Built-in AI form exporter, importer, user-context vault, autonomous filler, and page observer.",
  "permissions": [
    "sidePanel",
    "activeTab",
    "scripting",
    "storage"
  ],
  "host_permissions": [
    "<all_urls>"
  ],
  "background": {
    "service_worker": "service-worker.js",
    "type": "module"
  },
  "side_panel": {
    "default_path": "sidepanel.html"
  },
  "content_scripts": [
    {
      "matches": ["<all_urls>"],
      "js": ["content-script.js"],
      "run_at": "document_idle",
      "all_frames": true
    }
  ],
  "action": {
    "default_title": "Open LocalForm AI Side Panel"
  }
}`,

  'service-worker.js': `// ============================================================================
// Background Service Worker (Manifest V3)
// Opens Side Panel on toolbar click and routes messages between Content & Panel
// ============================================================================

chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((error) => console.error("SidePanel setup error:", error));

chrome.runtime.onInstalled.addListener(async () => {
  const existing = await chrome.storage.local.get(['userContextVault', 'pendingDeltas']);
  if (!existing.userContextVault) {
    await chrome.storage.local.set({
      userContextVault: ${JSON.stringify(INITIAL_CONTEXT_ENTRIES, null, 6)},
      pendingDeltas: []
    });
  }
});

// Relay passive form-submit observations from Content Script to storage queue
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'FORM_SUBMIT_DETECTED') {
    handleFormSubmitObservation(message.payload, sender.tab)
      .then((res) => sendResponse(res))
      .catch((err) => sendResponse({ error: err.message }));
    return true; // async response
  }
});

function normalizeForDedup(str) {
  return String(str || '')
    .toLowerCase()
    .replace(/^https?:\\/\\/(www\\.)?/, '')
    .replace(/[^a-z0-9]/g, '');
}

async function handleFormSubmitObservation(payload, tab) {
  const { userContextVault = [], pendingDeltas = [], activeSessionContext = null } = await chrome.storage.local.get([
    'userContextVault',
    'pendingDeltas',
    'activeSessionContext'
  ]);

  const newDeltas = [];
  const sensitiveRegex = /password|passcode|cvv|cvc|cc-number|card-number|ssn|social-security/i;

  for (const field of payload.filledFormFields || []) {
    if (!field.value || typeof field.value !== 'string') continue;
    if (sensitiveRegex.test(field.query) || sensitiveRegex.test(field.locator?.name || '')) continue;

    const propText = String(field.value).trim();
    const normProp = normalizeForDedup(propText);
    if (!normProp || normProp.length < 2) continue;

    const alreadyInVault = userContextVault.some((v) => {
      const normVault = normalizeForDedup(v.value);
      return normVault === normProp || (normProp.length > 10 && normVault.includes(normProp));
    });
    if (alreadyInVault) continue;

    const alreadyInPending = pendingDeltas.some((d) => {
      const normD = normalizeForDedup(d.proposedValue);
      return normD === normProp || (normProp.length > 10 && normD.includes(normProp));
    });
    if (alreadyInPending) continue;

    let key = field.query;
    let category = 'custom';

    if (/linkedin\\.com/i.test(propText) && /github/i.test(key)) {
      key = 'LinkedIn Profile URL';
      category = 'social';
    } else if (/github\\.com/i.test(propText) && /linkedin/i.test(key)) {
      key = 'GitHub Profile URL';
      category = 'social';
    }

    const pageTitle = payload.title || 'active page';
    const pageUrl = payload.url || (tab && tab.url) || '';

    const isCompanySpecificCoverLetter =
      /i am applying for|writing to apply|role at |position at /i.test(propText) ||
      /cover letter/i.test(key);

    if (isCompanySpecificCoverLetter) {
      const companyOrg = activeSessionContext?.organization || pageTitle.split('|')[0].trim() || 'Recent Application';
      newDeltas.push({
        deltaId: 'delta_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
        action: 'create',
        category: 'custom',
        key: \`Cover Letter Sample (\${companyOrg})\`,
        proposedValue: propText,
        evidenceSnippet: \`Submitted on form "\${pageTitle}"\`,
        sourceUrl: pageUrl,
        confidence: 0.9
      });
      continue;
    }

    const normalizedQuery = key.toLowerCase().trim();
    const existing = userContextVault.find((e) => {
      if (e.key.toLowerCase() === normalizedQuery) return true;
      if (e.aliases && e.aliases.some((a) => normalizedQuery.includes(a.toLowerCase()))) return true;
      if (field.constraints?.autocomplete && e.autocomplete === field.constraints.autocomplete) return true;
      return false;
    });

    if (!existing) {
      newDeltas.push({
        deltaId: 'delta_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
        action: 'create',
        category,
        key,
        proposedValue: propText,
        evidenceSnippet: \`Submitted on form "\${pageTitle}"\`,
        sourceUrl: pageUrl,
        confidence: 0.88
      });
    } else if (existing.value !== propText) {
      if (existing.id === 'custom.bio') {
        newDeltas.push({
          deltaId: 'delta_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
          action: 'create',
          category: 'custom',
          key: \`Cover Letter / Bio (\${pageTitle.split('|')[0].trim()})\`,
          proposedValue: propText,
          evidenceSnippet: \`Submitted on form "\${pageTitle}"\`,
          sourceUrl: pageUrl,
          confidence: 0.88
        });
      } else {
        newDeltas.push({
          deltaId: 'delta_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
          action: 'update',
          targetEntryId: existing.id,
          category: existing.category,
          key: existing.key,
          previousValue: existing.value,
          proposedValue: propText,
          evidenceSnippet: \`Updated value on "\${pageTitle}"\`,
          sourceUrl: pageUrl,
          confidence: 0.94
        });
      }
    }
  }

  if (newDeltas.length > 0) {
    const merged = [...newDeltas, ...pendingDeltas].slice(0, 50);
    await chrome.storage.local.set({ pendingDeltas: merged });
    chrome.action.setBadgeText({ text: String(merged.length) });
    chrome.action.setBadgeBackgroundColor({ color: '#10b981' });
  }

  return { queuedDeltas: newDeltas.length };
}`,

  'content-script.js': `// ============================================================================
// Content Script (Phases 2, 3, 4, 6, 7)
// Handles DOM Form Scanning, Ashby/React-Select Comboboxes, Yes/No Pills & Observer
// ============================================================================

(() => {
  if (window.__localFormAIInjected) return;
  window.__localFormAIInjected = true;

  // Prevent captcha/payment/analytics iframes from hijacking tab message broadcasts
  const BLOCKED_IFRAME_HOSTS = [
    'recaptcha.net',
    'google.com/recaptcha',
    'gstatic.com/recaptcha',
    'hcaptcha.com',
    'challenges.cloudflare.com',
    'stripe.com',
    'doubleclick.net',
    'googletagmanager.com'
  ];
  if (
    window !== window.top &&
    BLOCKED_IFRAME_HOSTS.some((domain) => window.location.href.includes(domain))
  ) {
    return;
  }

  const SENSITIVE_PATTERNS = /password|passcode|cvv|cvc|cc-number|card-number|ssn|social-security/i;
  const CAPTCHA_FIELD_PATTERNS = /g-recaptcha-response|h-captcha-response|cf-turnstile-response|recaptcha/i;

  function cleanLabelText(raw) {
    return (raw || '').replace(/\\s+/g, ' ').replace(/\\*+$/, '').trim();
  }

  // 1. Resolve human-readable label for any form input (Ashby comboboxes + standard forms)
  function resolveFieldLabel(el) {
    // Priority 1: aria-labelledby
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const texts = labelledBy
        .split(/\\s+/)
        .map((id) => document.getElementById(id)?.innerText?.trim())
        .filter(Boolean);
      if (texts.length) return cleanLabelText(texts.join(' '));
    }

    // Priority 2: aria-label
    const ariaLabel = el.getAttribute('aria-label')?.trim();
    if (ariaLabel) return cleanLabelText(ariaLabel);

    // Priority 3: Explicit <label for="id">
    if (el.id) {
      const explicitLabel = document.querySelector(\`label[for="\${CSS.escape(el.id)}"]\`);
      if (explicitLabel?.innerText?.trim()) return cleanLabelText(explicitLabel.innerText);
    }

    // Priority 4: Wrapping <label>
    const parentLabel = el.closest('label');
    if (parentLabel) {
      const clone = parentLabel.cloneNode(true);
      clone.querySelectorAll('input, select, textarea, button').forEach((n) => n.remove());
      const text = cleanLabelText(clone.innerText);
      if (text) return text;
    }

    // Priority 5: Walk up to 5 ancestor levels for Ashby/Lever/Workday field containers
    // (Important: Runs BEFORE placeholder so "Start typing..." doesn't mask "Where are you currently based?")
    let curr = el;
    for (let depth = 0; depth < 5 && curr && curr !== document.body; depth++) {
      const fieldLabel = curr.querySelector('label, [class*="label"], [class*="Label"], legend');
      if (fieldLabel && !fieldLabel.contains(el) && fieldLabel.innerText?.trim()) {
        return cleanLabelText(fieldLabel.innerText).slice(0, 160);
      }
      const prev = curr.previousElementSibling;
      if (prev && ['LABEL', 'SPAN', 'DIV', 'P'].includes(prev.tagName) && prev.innerText?.trim()) {
        return cleanLabelText(prev.innerText).slice(0, 160);
      }
      curr = curr.parentElement;
    }

    // Priority 6: Placeholder
    const placeholder = el.getAttribute('placeholder')?.trim();
    if (placeholder) return cleanLabelText(placeholder);

    // Priority 7: Name or ID fallback
    return cleanLabelText(el.name || el.id || 'Unnamed Field');
  }

  // 2. Build unique, resilient CSS selector
  function buildCssSelector(el) {
    if (el.id) return \`#\${CSS.escape(el.id)}\`;
    if (el.name) {
      const tag = el.tagName.toLowerCase();
      const byName = document.querySelectorAll(\`\${tag}[name="\${CSS.escape(el.name)}"]\`);
      if (byName.length === 1) return \`\${tag}[name="\${CSS.escape(el.name)}"]\`;
    }
    const path = [];
    let current = el;
    while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.body) {
      let selector = current.tagName.toLowerCase();
      if (current.id) {
        selector += \`#\${CSS.escape(current.id)}\`;
        path.unshift(selector);
        break;
      } else {
        let sib = current;
        let nth = 1;
        while ((sib = sib.previousElementSibling)) {
          if (sib.tagName === current.tagName) nth++;
        }
        if (nth > 1) selector += \`:nth-of-type(\${nth})\`;
      }
      path.unshift(selector);
      current = current.parentElement;
    }
    return path.join(' > ');
  }

  // 3. Nearest section heading or fieldset legend (Phase 4 Form-Export++)
  function findNearestSectionHeading(el) {
    const fieldset = el.closest('fieldset');
    if (fieldset) {
      const legend = fieldset.querySelector('legend');
      if (legend?.innerText?.trim()) return legend.innerText.trim();
    }
    let node = el;
    while (node && node !== document.body) {
      let prev = node.previousElementSibling;
      while (prev) {
        if (/^H[1-4]$/.test(prev.tagName) && prev.innerText?.trim()) {
          return prev.innerText.trim();
        }
        prev = prev.previousElementSibling;
      }
      node = node.parentElement;
    }
    return undefined;
  }

  // 4. Scan Current Page Forms (Phase 2 & Phase 4)
  function scanCurrentPageForms(options = { includePageContext: false, includeCurrentValues: true }) {
    const rawElements = Array.from(
      document.querySelectorAll('input, textarea, select, [contenteditable="true"]')
    );

    const radioGroups = new Map();
    const fields = [];

    rawElements.forEach((el, idx) => {
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute('type') || (tag === 'textarea' ? 'textarea' : tag)).toLowerCase();

      // Skip non-data inputs and sensitive fields
      if (['hidden', 'submit', 'button', 'reset', 'image', 'file', 'password'].includes(type)) return;
      if (el.disabled || el.getAttribute('aria-hidden') === 'true') return;

      // Skip hidden captcha textareas (e.g., g-recaptcha-response-100000)
      if (
        CAPTCHA_FIELD_PATTERNS.test(el.id || '') ||
        CAPTCHA_FIELD_PATTERNS.test(el.name || '')
      ) {
        return;
      }

      const style = window.getComputedStyle(el);
      if (type !== 'checkbox' && type !== 'radio' && (style.display === 'none' || style.visibility === 'hidden')) {
        return;
      }

      const labelQuery = resolveFieldLabel(el);
      if (
        SENSITIVE_PATTERNS.test(labelQuery) ||
        SENSITIVE_PATTERNS.test(el.name || '') ||
        SENSITIVE_PATTERNS.test(el.autocomplete || '') ||
        CAPTCHA_FIELD_PATTERNS.test(labelQuery)
      ) {
        return;
      }

      // Coalesce radio groups
      if (type === 'radio' && el.name) {
        if (!radioGroups.has(el.name)) {
          const groupLegend = findNearestSectionHeading(el) || el.name;
          const groupField = {
            fieldId: \`field_radio_\${el.name}\`,
            query: groupLegend,
            inputType: 'radio-group',
            value: null,
            locator: {
              cssSelector: \`input[type="radio"][name="\${CSS.escape(el.name)}"]\`,
              name: el.name
            },
            constraints: {
              required: el.required,
              options: []
            },
            sectionHeading: findNearestSectionHeading(el)
          };
          radioGroups.set(el.name, groupField);
          fields.push(groupField);
        }
        const group = radioGroups.get(el.name);
        const optLabel = resolveFieldLabel(el);
        group.constraints.options.push({
          label: optLabel,
          value: el.value,
          selected: el.checked
        });
        if (el.checked && options.includeCurrentValues) {
          group.value = el.value;
        }
        return;
      }

      const cssSelector = buildCssSelector(el);
      let currentValue = null;
      if (options.includeCurrentValues) {
        if (type === 'checkbox') {
          const container = el.closest('[class*="fieldEntry"], [class*="form-field"], fieldset') || el.parentElement?.parentElement;
          const activeBtn = container?.querySelector('button[aria-pressed="true"], button[class*="selected"], button[class*="active"]');
          if (activeBtn) {
            currentValue = /^yes$/i.test(activeBtn.textContent.trim());
          } else {
            currentValue = el.checked ? true : null;
          }
        } else if (tag === 'select' && el.multiple) {
          currentValue = Array.from(el.selectedOptions).map((o) => o.value);
        } else if (el.isContentEditable) {
          currentValue = el.innerText.trim();
        } else {
          currentValue = el.value || '';
        }
      }

      const selectOptions =
        tag === 'select'
          ? Array.from(el.options).map((o) => ({
              label: o.text.trim(),
              value: o.value,
              selected: o.selected
            }))
          : undefined;

      fields.push({
        fieldId: \`field_\${idx}_\${(el.id || el.name || 'input').replace(/[^a-zA-Z0-9_-]/g, '')}\`,
        query: labelQuery,
        inputType: tag === 'select' ? (el.multiple ? 'select-multiple' : 'select-one') : type === 'input' ? 'text' : type,
        value: currentValue,
        locator: {
          cssSelector,
          id: el.id || undefined,
          name: el.name || undefined,
          ariaLabel: el.getAttribute('aria-label') || undefined
        },
        constraints: {
          required: Boolean(el.required),
          placeholder: el.getAttribute('placeholder') || undefined,
          autocomplete: el.getAttribute('autocomplete') || undefined,
          maxLength: el.maxLength > 0 ? el.maxLength : undefined,
          options: selectOptions
        },
        sectionHeading: options.includePageContext ? findNearestSectionHeading(el) : undefined
      });
    });

    const exportDoc = {
      schemaVersion: options.includePageContext ? '2.0' : '1.0',
      exportedAt: new Date().toISOString(),
      fields
    };

    if (options.includePageContext) {
      const headings = Array.from(document.querySelectorAll('h1, h2, h3'))
        .map((h) => h.innerText?.trim())
        .filter(Boolean)
        .slice(0, 12);
      const metaDesc = document.querySelector('meta[name="description"]')?.getAttribute('content') || '';
      const rawBodyExcerpt = document.body.innerText.replace(/\\s+/g, ' ').trim().slice(0, 3500);

      exportDoc.pageContext = {
        url: window.location.href,
        domain: window.location.hostname,
        title: document.title,
        description: metaDesc,
        headings,
        rawTextExcerpt: rawBodyExcerpt
      };
    }

    return exportDoc;
  }

  // 5. React / Vue / Ashby / Typeahead Compatible Native Value Setter (Phase 3 & 6)
  function setReactSafeValue(el, value) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();

    if (type === 'checkbox') {
      const strVal = String(value).trim().toLowerCase();
      const shouldCheck = value === true || strVal === 'true' || strVal === 'yes' || strVal === '1';
      const targetButtonText = shouldCheck ? 'yes' : 'no';

      // Ashby HQ Pattern: Check if parent container uses <button>Yes</button> / <button>No</button> pills
      let container = el.parentElement;
      let clickedPill = false;
      for (let d = 0; d < 4 && container && container !== document.body; d++) {
        const pillButtons = Array.from(container.querySelectorAll('button, [role="button"], label'));
        const matchingPill = pillButtons.find(
          (btn) => btn.innerText?.trim().toLowerCase() === targetButtonText
        );
        if (matchingPill) {
          matchingPill.click();
          clickedPill = true;
          break;
        }
        container = container.parentElement;
      }

      if (!clickedPill && el.checked !== shouldCheck) {
        el.click();
        el.checked = shouldCheck;
      }
    } else if (tag === 'select') {
      const strVal = String(value).toLowerCase();
      const matchedOption = Array.from(el.options).find(
        (opt) =>
          opt.value.toLowerCase() === strVal ||
          opt.text.trim().toLowerCase() === strVal ||
          opt.text.trim().toLowerCase().includes(strVal)
      );
      if (matchedOption) {
        el.value = matchedOption.value;
      }
    } else if (el.isContentEditable) {
      el.innerText = String(value);
    } else {
      el.focus();
      const prototype = Object.getPrototypeOf(el);
      const prototypeSetter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
      const ownSetter = Object.getOwnPropertyDescriptor(el, 'value')?.set;

      if (prototypeSetter && ownSetter !== prototypeSetter) {
        prototypeSetter.call(el, String(value));
      } else if (ownSetter) {
        ownSetter.call(el, String(value));
      } else {
        el.value = String(value);
      }

      // If this is a combobox/typeahead (like Ashby's location picker), select the first dropdown suggestion
      const isTypeahead =
        el.getAttribute('role') === 'combobox' ||
        el.getAttribute('placeholder')?.toLowerCase().includes('start typing') ||
        el.getAttribute('aria-autocomplete') === 'list';

      if (isTypeahead) {
        el.focus();
        el.dispatchEvent(new KeyboardEvent('keydown', { key: 'a', bubbles: true }));
        setTimeout(() => {
          const listbox = document.querySelector('[role="listbox"], [class*="menu"], [class*="options"]');
          const firstOpt = listbox
            ? listbox.querySelector('[role="option"], [class*="option"], li, div[id*="option"]')
            : document.querySelector('[role="option"], [class*="option"], [class*="Option"], [class*="menu"] li');
          if (firstOpt) {
            firstOpt.click();
          } else {
            el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true }));
          }
        }, 300);
      }
    }

    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
  }

  function isVisibleInput(el) {
    if (!el || el.disabled || el.getAttribute('aria-hidden') === 'true') return false;
    const style = window.getComputedStyle(el);
    if (el.type !== 'checkbox' && el.type !== 'radio' && (style.display === 'none' || style.visibility === 'hidden')) {
      return false;
    }
    return true;
  }

  function locateElement(field) {
    const loc = field.locator || {};

    // 1. Direct document.getElementById (immune to CSS selector escaping bugs on numeric UUIDs like "87368112-...")
    if (loc.id) {
      const byId = document.getElementById(loc.id);
      if (byId) return byId;
    }

    // 2. Direct name attribute lookup via CSS.escape
    if (loc.name) {
      try {
        const byName = document.querySelector(\`[name="\${CSS.escape(loc.name)}"]\`);
        if (byName) return byName;
      } catch (_) {}
    }

    // 3. CSS Selector lookup
    if (loc.cssSelector) {
      try {
        const byCss = document.querySelector(loc.cssSelector);
        if (byCss) return byCss;
      } catch (_) {}
    }

    // 4. Fallback: Match by resolved human-readable label text (\`field.query\`)!
    // Crucial when React re-renders DOM wrappers after "Autofill from resume"
    if (field.query) {
      const targetQ = field.query.toLowerCase().replace(/\\s+/g, ' ').trim();
      const candidates = Array.from(
        document.querySelectorAll('input:not([type="hidden"]):not([type="file"]), textarea, select, [contenteditable="true"]')
      ).filter(isVisibleInput);

      for (const el of candidates) {
        const elLabel = resolveFieldLabel(el).toLowerCase().replace(/\\s+/g, ' ').trim();
        if (elLabel === targetQ || (targetQ.length > 8 && (elLabel.includes(targetQ) || targetQ.includes(elLabel)))) {
          return el;
        }
      }
    }

    return null;
  }

  // 6. Hydrate Page Forms from Export Document (Phase 3 & Phase 6)
  function hydratePageForms(doc, options = { highlightConfidence: true }) {
    let filledCount = 0;
    const failedFields = [];

    for (const field of doc.fields || []) {
      if (field.value === null || field.value === undefined || field.value === '') continue;

      try {
        if (field.inputType === 'radio-group' && field.locator?.name) {
          const radios = Array.from(
            document.querySelectorAll(\`input[type="radio"][name="\${CSS.escape(field.locator.name)}"]\`)
          );
          const targetVal = String(field.value).toLowerCase();
          const matchedRadio = radios.find(
            (r) =>
              r.value.toLowerCase() === targetVal ||
              resolveFieldLabel(r).toLowerCase().includes(targetVal)
          );
          if (matchedRadio) {
            matchedRadio.click();
            matchedRadio.checked = true;
            matchedRadio.dispatchEvent(new Event('change', { bubbles: true }));
            filledCount++;
          } else {
            failedFields.push({ fieldId: field.fieldId, query: field.query, reason: 'Radio option not found' });
          }
          continue;
        }

        // Resilient element lookup: ID -> name -> CSS selector -> resolved label text
        const el = locateElement(field);

        if (!el) {
          failedFields.push({ fieldId: field.fieldId, query: field.query, reason: 'Element not found in DOM' });
          continue;
        }

        setReactSafeValue(el, field.value);
        filledCount++;

        if (options.highlightConfidence) {
          const conf = typeof field.confidence === 'number' ? field.confidence : 1.0;
          const borderColor = conf >= 0.8 ? '#10b981' : '#f59e0b';
          el.style.transition = 'box-shadow 0.25s ease, outline 0.25s ease';
          el.style.outline = \`2px solid \${borderColor}\`;
          el.style.boxShadow = \`0 0 0 4px \${conf >= 0.8 ? 'rgba(16,185,129,0.18)' : 'rgba(245,158,11,0.18)'}\`;
        }
      } catch (err) {
        failedFields.push({ fieldId: field.fieldId, query: field.query, reason: err.message });
      }
    }

    return {
      totalFields: (doc.fields || []).length,
      filledCount,
      failedFields
    };
  }

  // 7. Capture Page Snapshot for Phase 7 Observer
  function capturePageSnapshot() {
    const formScan = scanCurrentPageForms({ includePageContext: true, includeCurrentValues: true });
    const filledFormFields = formScan.fields.filter(
      (f) => f.value !== null && f.value !== '' && f.value !== false
    );
    const cleanText = document.body.innerText.replace(/\\s+/g, ' ').trim().slice(0, 5000);

    return {
      url: window.location.href,
      title: document.title,
      extractedText: cleanText,
      filledFormFields
    };
  }

  // Listen for passive form submissions to propose context updates
  document.addEventListener(
    'submit',
    () => {
      try {
        const snapshot = capturePageSnapshot();
        if (snapshot.filledFormFields.length > 0) {
          chrome.runtime.sendMessage({
            action: 'FORM_SUBMIT_DETECTED',
            payload: snapshot
          });
        }
      } catch (e) {}
    },
    true
  );

  // RPC Listener from Side Panel
  chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
    if (msg.action === 'SCAN_PAGE_FORMS') {
      sendResponse(scanCurrentPageForms(msg.options || {}));
    } else if (msg.action === 'HYDRATE_PAGE_FORMS') {
      sendResponse(hydratePageForms(msg.document, msg.options || {}));
    } else if (msg.action === 'CAPTURE_PAGE_SNAPSHOT') {
      sendResponse(capturePageSnapshot());
    }
    return true;
  });
})();`,

  'sidepanel.html': `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>LocalForm AI — Side Panel</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --border: #334155;
      --text: #f8fafc;
      --muted: #94a3b8;
      --accent: #10b981;
      --indigo: #6366f1;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 12px;
      font-size: 13px;
      line-height: 1.45;
    }
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 12px;
    }
    .nav {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 4px;
      margin-bottom: 12px;
    }
    .nav button {
      background: var(--card);
      color: var(--muted);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 4px;
      font-size: 11px;
      cursor: pointer;
      font-weight: 600;
    }
    .nav button.active {
      background: var(--indigo);
      color: white;
      border-color: var(--indigo);
    }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 12px;
      margin-bottom: 10px;
    }
    .btn {
      width: 100%;
      padding: 8px 12px;
      border-radius: 6px;
      border: none;
      font-weight: 600;
      cursor: pointer;
      background: var(--accent);
      color: #052e16;
      margin-top: 8px;
    }
    .btn-secondary {
      background: var(--indigo);
      color: white;
    }
    pre {
      background: #020617;
      padding: 8px;
      border-radius: 6px;
      overflow-x: auto;
      font-size: 11px;
      max-height: 220px;
      margin-top: 8px;
    }
    input, textarea {
      width: 100%;
      background: #020617;
      border: 1px solid var(--border);
      color: white;
      padding: 6px 8px;
      border-radius: 6px;
      margin-top: 4px;
    }
    @keyframes lf-spin { to { transform: rotate(360deg); } }
    .lf-spinner {
      width: 11px;
      height: 11px;
      border: 2px solid #3b82f6;
      border-top-color: transparent;
      border-radius: 50%;
      display: inline-block;
      animation: lf-spin 0.75s linear infinite;
    }
  </style>
</head>
<body>
  <div class="header">
    <strong>LocalForm AI (Phases 1–7)</strong>
    <span id="ai-badge" style="font-size:11px; color:#94a3b8;">Checking AI...</span>
  </div>

  <div id="ux-activity-bar" style="display:none; background:#0f172a; border-bottom:1px solid #3b82f6; padding:8px 12px; font-size:11px; margin:-12px -12px 12px -12px; position:sticky; top:0; z-index:100;">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:4px;">
      <div style="display:flex; align-items:center; gap:6px;">
        <span class="lf-spinner"></span>
        <strong id="ux-stage-label" style="color:#93c5fd;">Initializing...</strong>
      </div>
      <div style="display:flex; align-items:center; gap:8px;">
        <span id="ux-elapsed-timer" style="font-family:monospace; color:#38bdf8;">0.0s</span>
        <button id="btn-abort-ai" class="btn" style="width:auto; padding:1px 6px; font-size:10px; margin:0; background:#ef4444; color:white;">Abort</button>
      </div>
    </div>
    <div style="width:100%; height:4px; background:#1e293b; border-radius:2px; overflow:hidden;">
      <div id="ux-progress-fill" style="width:15%; height:100%; background:linear-gradient(90deg, #3b82f6, #10b981); transition:width 0.2s ease;"></div>
    </div>
    <div id="ux-stream-preview" style="font-family:monospace; font-size:10px; color:#94a3b8; margin-top:4px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;"></div>
  </div>

  <div class="nav">
    <button data-tab="phase1" class="active">1. AI Model</button>
    <button data-tab="phase24">2/4. Export</button>
    <button data-tab="phase36">3/6. Fill++</button>
    <button data-tab="phase57">5/7. Vault</button>
  </div>

  <!-- PHASE 1: Built-in AI Readiness -->
  <section id="tab-phase1" class="tab-pane">
    <div class="card">
      <h4>Phase 1: Chrome Built-in AI Readiness</h4>
      <p style="color:var(--muted); margin-top:4px;">Detects on-device Gemini Nano availability (LanguageModel & Summarizer APIs).</p>
      <pre id="model-status-box">Querying API status...</pre>
      <button id="btn-download-model" class="btn">Download / Bootstrap Local Models</button>
    </div>
  </section>

  <!-- PHASE 2 & 4: Form-Export & Form-Export++ -->
  <section id="tab-phase24" class="tab-pane" style="display:none;">
    <div class="card">
      <h4>Phase 2 & 4: Form-Export / Form-Export++</h4>
      <label style="display:flex; align-items:center; gap:6px; margin-top:8px;">
        <input type="checkbox" id="chk-export-plus" checked style="width:auto;" />
        Include Page Context & AI Summary (Form-Export++)
      </label>
      <button id="btn-scan-export" class="btn">Scan Active Tab & Export JSON</button>
      <pre id="export-preview-box">Click scan to inspect form schema...</pre>
    </div>
  </section>

  <!-- PHASE 3 & 6: Form-Import & Form-Import++ -->
  <section id="tab-phase36" class="tab-pane" style="display:none;">
    <div class="card">
      <h4>Phase 6: Form-Import++ (1-Click Autonomous Fill)</h4>
      <p style="color:var(--muted); margin-top:4px;">Scans page, matches User-Context via local Prompt API, and hydrates fields.</p>
      <button id="btn-autofill-plus" class="btn">✨ Auto-Fill Current Form with AI</button>
      <pre id="autofill-status-box">Ready.</pre>
    </div>
    <div class="card">
      <h4>Phase 3: Manual Form-Import (JSON File)</h4>
      <input type="file" id="file-import-json" accept=".json" />
      <button id="btn-import-json" class="btn btn-secondary">Hydrate Form from JSON</button>
    </div>
  </section>

  <!-- PHASE 5 & 7: User-Context Vault & Page Observer -->
  <section id="tab-phase57" class="tab-pane" style="display:none;">
    <div class="card">
      <h4>Phase 7: Observer — Target Context & Profile Learning</h4>
      <div style="display:flex; gap:6px; margin-bottom:8px;">
        <button id="btn-capture-target-context" class="btn btn-primary" style="flex:1;">🎯 Capture Target / Job Context</button>
        <button id="btn-snapshot-user-facts" class="btn btn-secondary" style="flex:1;">👤 Extract My Profile Facts</button>
      </div>
      <div id="session-context-card" style="display:none; border:1px solid #3b82f6; padding:8px; border-radius:6px; margin-bottom:8px; background:rgba(30,58,138,0.15);">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <strong style="font-size:11px; color:#93c5fd;">🎯 Active Target Context</strong>
          <button id="btn-clear-target-context" class="btn btn-danger btn-sm" style="padding:2px 6px;">Clear</button>
        </div>
        <pre id="session-context-summary" style="font-size:10.5px; margin-top:4px; white-space:pre-wrap;"></pre>
      </div>
      <div id="observer-deltas-list" style="margin-top:8px;"></div>
    </div>
    <div class="card">
      <h4>Phase 5: User-Context Vault</h4>
      <div id="vault-entries-list" style="margin-top:8px;"></div>
      <input type="text" id="new-fact-key" placeholder="Fact Label (e.g., GitHub URL)" />
      <input type="text" id="new-fact-val" placeholder="Fact Value" />
      <button id="btn-add-fact" class="btn">Add Context Entry</button>
    </div>
  </section>

  <!-- Persistent Observability & Telemetry Footer Drawer -->
  <div id="obs-drawer" style="border-top:1px solid #1e293b; background:#090d16; font-size:11px; margin:16px -12px -12px -12px; position:sticky; bottom:0; z-index:90;">
    <div id="obs-drawer-toggle" style="display:flex; justify-content:space-between; align-items:center; padding:6px 12px; cursor:pointer; user-select:none;">
      <span style="color:#94a3b8; font-weight:600;">📡 Observability & AI Trace (<span id="obs-log-count">0</span> events)</span>
      <span id="obs-token-pill" style="font-family:monospace; font-size:10px; color:#64748b;">Tokens: 0 / 9216</span>
    </div>
    <div id="obs-drawer-body" style="display:none; max-height:190px; overflow-y:auto; padding:8px 12px; border-top:1px solid #1e293b; font-family:monospace; font-size:10px;">
      <div style="display:flex; justify-content:flex-end; margin-bottom:4px;">
        <button id="btn-clear-traces" style="background:none; border:none; color:#64748b; cursor:pointer; font-size:10px;">Clear Logs</button>
      </div>
      <div id="obs-trace-list"></div>
    </div>
  </div>

  <script type="module" src="sidepanel.js"></script>
</body>
</html>`,

  'sidepanel.js': `// ============================================================================
// Side Panel Controller (Phases 1 through 7)
// Interfaces directly with Chrome Built-in AI APIs (LanguageModel, Summarizer)
// ============================================================================

let baseWarmSession = null;

async function getClonedSession() {
  const lmGlobal = self.LanguageModel || self.ai?.languageModel;
  if (!lmGlobal) return null;
  try {
    if (!baseWarmSession) {
      baseWarmSession = await lmGlobal.create({
        outputLanguage: 'en',
        systemPrompt: 'You are an expert technical career assistant for Dr. Ariel Anders (MIT PhD Roboticist, former Waymo Motion Planning Senior SWE, Robust.AI first roboticist, Civ Robotics Senior Algorithms Engineer). Always write tailored, concrete, first-person responses.'
      });
    }
    if (typeof baseWarmSession.clone === 'function') {
      return await baseWarmSession.clone();
    }
    return await lmGlobal.create({ outputLanguage: 'en' });
  } catch (e) {
    baseWarmSession = null;
    return await lmGlobal.create({ outputLanguage: 'en' });
  }
}

async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function sendToMainFrame(tabId, payload) {
  // Target frameId: 0 first so invisible reCAPTCHA iframes never win the broadcast race
  return await chrome.tabs.sendMessage(tabId, payload, { frameId: 0 });
}

function escapeHtml(str) {
  return String(str || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

const Telemetry = {
  traces: [],
  timerInterval: null,
  startTime: 0,
  activeAbortController: null,

  startTask(stageLabel, progressPct = 15) {
    this.startTime = performance.now();
    this.activeAbortController = new AbortController();

    const bar = document.getElementById('ux-activity-bar');
    const lbl = document.getElementById('ux-stage-label');
    const fill = document.getElementById('ux-progress-fill');
    const timerEl = document.getElementById('ux-elapsed-timer');
    const previewEl = document.getElementById('ux-stream-preview');

    if (bar) bar.style.display = 'block';
    if (lbl) lbl.textContent = stageLabel;
    if (fill) fill.style.width = \`\${progressPct}%\`;
    if (previewEl) previewEl.textContent = '';

    clearInterval(this.timerInterval);
    this.timerInterval = setInterval(() => {
      const elapsedSec = ((performance.now() - this.startTime) / 1000).toFixed(1);
      if (timerEl) timerEl.textContent = \`\${elapsedSec}s\`;
    }, 100);

    return this.activeAbortController.signal;
  },

  updateStage(stageLabel, progressPct, streamSnippet = '') {
    const lbl = document.getElementById('ux-stage-label');
    const fill = document.getElementById('ux-progress-fill');
    const previewEl = document.getElementById('ux-stream-preview');
    if (lbl) lbl.textContent = stageLabel;
    if (fill && progressPct !== undefined) fill.style.width = \`\${progressPct}%\`;
    if (previewEl && streamSnippet) {
      previewEl.textContent = \`⚡ \${streamSnippet.slice(-90)}\`;
    }
  },

  endTask() {
    clearInterval(this.timerInterval);
    const bar = document.getElementById('ux-activity-bar');
    const fill = document.getElementById('ux-progress-fill');
    if (fill) fill.style.width = '100%';
    setTimeout(() => {
      if (bar) bar.style.display = 'none';
    }, 450);
  },

  updateTokenUsage(session) {
    if (!session) return;
    const used = session.contextUsage ?? session.tokensSoFar ?? 0;
    const max = session.contextWindow ?? session.maxTokens ?? 9216;
    const pill = document.getElementById('obs-token-pill');
    if (pill) {
      const pct = Math.round((used / max) * 100);
      pill.textContent = \`Tokens: \${used} / \${max} (\${pct}%)\`;
      pill.style.color = pct > 80 ? '#f87171' : '#38bdf8';
    }
  },

  logTrace({ type, title, durationMs, details }) {
    const entry = {
      id: Date.now() + Math.random(),
      ts: new Date().toLocaleTimeString(),
      type,
      title,
      durationMs: Math.round(durationMs || 0),
      details: typeof details === 'string' ? details : JSON.stringify(details, null, 2)
    };
    this.traces.unshift(entry);
    if (this.traces.length > 40) this.traces.pop();
    this.renderTraces();
  },

  renderTraces() {
    const countEl = document.getElementById('obs-log-count');
    const listEl = document.getElementById('obs-trace-list');
    if (countEl) countEl.textContent = String(this.traces.length);
    if (!listEl) return;

    const colorMap = {
      SCAN: '#38bdf8',
      HYDRATE: '#10b981',
      LLM_PROMPT: '#a855f7',
      OBSERVER: '#f59e0b',
      ERROR: '#ef4444'
    };

    listEl.innerHTML = this.traces
      .map(
        (t) => \`
      <details style="margin-bottom:6px; border-left:2px solid \${colorMap[t.type] || '#64748b'}; padding-left:6px;">
        <summary style="cursor:pointer; color:#e2e8f0; display:flex; justify-content:space-between;">
          <span><strong style="color:\${colorMap[t.type] || '#94a3b8'}">[\${t.type}]</strong> \${escapeHtml(t.title)}</span>
          <span style="color:#64748b;">\${t.durationMs}ms · \${t.ts}</span>
        </summary>
        <pre style="margin:4px 0 0; padding:6px; background:#020617; color:#94a3b8; border-radius:4px; overflow-x:auto; white-space:pre-wrap;">\${escapeHtml(t.details)}</pre>
      </details>
    \`
      )
      .join('');
  }
};

document.getElementById('obs-drawer-toggle')?.addEventListener('click', () => {
  const body = document.getElementById('obs-drawer-body');
  if (body) body.style.display = body.style.display === 'none' ? 'block' : 'none';
});
document.getElementById('btn-clear-traces')?.addEventListener('click', (e) => {
  e.stopPropagation();
  Telemetry.traces = [];
  Telemetry.renderTraces();
});
document.getElementById('btn-abort-ai')?.addEventListener('click', () => {
  Telemetry.activeAbortController?.abort();
  Telemetry.logTrace({ type: 'ERROR', title: 'User Aborted AI Task', durationMs: 0, details: 'AbortController triggered' });
  Telemetry.endTask();
});

async function runObservablePrompt(session, prompt, options = {}) {
  const t0 = performance.now();
  const { label = 'Gemini Nano Inference', responseConstraint, signal, onChunk } = options;

  try {
    let finalOutput = '';
    const promptOpts = {};
    if (responseConstraint) promptOpts.responseConstraint = responseConstraint;
    if (signal) promptOpts.signal = signal;

    if (typeof session.promptStreaming === 'function' && !responseConstraint) {
      const stream = session.promptStreaming(prompt, promptOpts);
      for await (const chunk of stream) {
        if (chunk.startsWith(finalOutput)) {
          finalOutput = chunk;
        } else {
          finalOutput += chunk;
        }
        Telemetry.updateStage(
          \`\${label} (\${finalOutput.length} chars)...\`,
          undefined,
          finalOutput
        );
        if (onChunk) onChunk(finalOutput);
      }
    } else {
      finalOutput = await session.prompt(prompt, promptOpts);
    }

    const durationMs = performance.now() - t0;
    Telemetry.updateTokenUsage(session);
    Telemetry.logTrace({
      type: 'LLM_PROMPT',
      title: \`\${label} (\${finalOutput.length} chars out)\`,
      durationMs,
      details: \`PROMPT (\${prompt.length} chars):\\n\${prompt.slice(0, 600)}...\\n\\nOUTPUT:\\n\${finalOutput}\`
    });

    return finalOutput;
  } catch (err) {
    Telemetry.logTrace({
      type: 'ERROR',
      title: \`\${label} Failed: \${err.message}\`,
      durationMs: performance.now() - t0,
      details: err.stack || err.message
    });
    throw err;
  }
}

// --- PHASE 1: Built-in AI Detection & Bootstrap ---
async function checkBuiltInAIStatus() {
  const report = {
    languageModel: 'unavailable',
    summarizer: 'unavailable',
    userAgent: navigator.userAgent
  };

  try {
    if ('LanguageModel' in self) {
      report.languageModel = await self.LanguageModel.availability();
    } else if (self.ai?.languageModel) {
      const cap = await self.ai.languageModel.capabilities();
      report.languageModel = cap.available;
    }
  } catch (e) {
    report.languageModelError = e.message;
  }

  try {
    if ('Summarizer' in self) {
      report.summarizer = await self.Summarizer.availability();
    }
  } catch (e) {
    report.summarizerError = e.message;
  }

  document.getElementById('model-status-box').textContent = JSON.stringify(report, null, 2);
  document.getElementById('ai-badge').textContent = \`AI: \${report.languageModel}\`;
  return report;
}

async function createPromptSession(onProgress, options = {}) {
  const monitor = (m) => {
    m.addEventListener('downloadprogress', (e) => {
      const pct = Math.round((e.loaded || 0) * 100);
      if (onProgress) onProgress(pct);
    });
  };

  const sessionOptions = {
    outputLanguage: 'en',
    monitor,
    ...options
  };

  if ('LanguageModel' in self) {
    return await self.LanguageModel.create(sessionOptions);
  } else if (self.ai?.languageModel) {
    return await self.ai.languageModel.create(sessionOptions);
  }
  throw new Error('Prompt API (LanguageModel) not available in this Chrome build.');
}

// --- Tab Switching ---
document.querySelectorAll('.nav button').forEach((btn) => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav button').forEach((b) => b.classList.remove('active'));
    document.querySelectorAll('.tab-pane').forEach((p) => (p.style.display = 'none'));
    btn.classList.add('active');
    document.getElementById('tab-' + btn.dataset.tab).style.display = 'block';
  });
});

// Phase 1 Button
document.getElementById('btn-download-model').addEventListener('click', async () => {
  const box = document.getElementById('model-status-box');
  try {
    box.textContent = 'Initializing model download...';
    const session = await createPromptSession((pct) => {
      box.textContent = \`Downloading Gemini Nano: \${pct}%\`;
    });
    const ping = await session.prompt('Reply with READY.');
    box.textContent = \`Model Ready! Test response: \${ping}\`;
    session.destroy();
    await checkBuiltInAIStatus();
  } catch (err) {
    box.textContent = 'Error: ' + err.message;
  }
});

// Phase 2 & 4 Button: Scan & Export
document.getElementById('btn-scan-export').addEventListener('click', async () => {
  const includePageContext = document.getElementById('chk-export-plus').checked;
  const preview = document.getElementById('export-preview-box');
  const tab = await getActiveTab();
  const doc = await sendToMainFrame(tab.id, {
    action: 'SCAN_PAGE_FORMS',
    options: { includePageContext, includeCurrentValues: true }
  });

  if (includePageContext && doc.pageContext?.rawTextExcerpt && 'Summarizer' in self) {
    try {
      const summarizer = await self.Summarizer.create({ type: 'key-points', length: 'short' });
      doc.pageContext.pageSummary = await summarizer.summarize(doc.pageContext.rawTextExcerpt);
      summarizer.destroy();
    } catch (e) {
      doc.pageContext.pageSummary = 'Summarizer skipped: ' + e.message;
    }
  }

  preview.textContent = JSON.stringify(doc, null, 2);

  // Trigger file download
  const blob = new Blob([JSON.stringify(doc, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = \`form-export-\${Date.now()}.json\`;
  a.click();
});

// Phase 6 Button: Autonomous Form-Import++
document.getElementById('btn-autofill-plus').addEventListener('click', async () => {
  const statusBox = document.getElementById('autofill-status-box');
  statusBox.textContent = 'Scanning page forms...';
  const tab = await getActiveTab();
  const doc = await sendToMainFrame(tab.id, {
    action: 'SCAN_PAGE_FORMS',
    options: { includePageContext: true, includeCurrentValues: false }
  });

  const { userContextVault = [] } = await chrome.storage.local.get(['userContextVault']);

  // Tier 1: Deterministic fast match with Rule A & Rule B
  const unresolvedFields = [];
  for (let i = 0; i < doc.fields.length; i++) {
    const field = doc.fields[i];

    // Rule A: Skip conditional follow-up fields when preceding boolean field is false / No
    if (/^(if so|if yes|if applicable|please specify if)/i.test((field.query || '').trim())) {
      const prevField = i > 0 ? doc.fields[i - 1] : null;
      if (!prevField || prevField.value === false || prevField.value === 'No' || prevField.value === null || prevField.value === '') {
        field.value = '';
        field.skipLLM = true;
        continue;
      }
    }

    if (field.skipLLM) continue;

    const q = field.query.toLowerCase().replace(/\\*+$/, '').trim();
    const isLongFormEssay =
      field.inputType === 'textarea' ||
      /cover letter|why do you|why are you|motivation|share something cool|something you've built|tell us about|describe a project|what failed/i.test(q);

    const matched = userContextVault.find(
      (entry) =>
        (field.constraints?.autocomplete && entry.autocomplete === field.constraints.autocomplete) ||
        entry.key.toLowerCase() === q ||
        (entry.aliases &&
          entry.aliases.some((a) => {
            const alias = a.toLowerCase();
            return q === alias || q.includes(alias) || alias.includes(q);
          }))
    );
    if (matched) {
      if (field.inputType === 'checkbox') {
        field.value = /^(yes|true|1)$/i.test(String(matched.value).trim());
      } else {
        field.value = matched.value;
      }
      field.confidence = 0.98;
    } else {
      unresolvedFields.push(field);
    }
  }

  // Tier 2: On-Device Prompt API for remaining ambiguous fields
  if (unresolvedFields.length > 0) {
    try {
      statusBox.textContent = \`Running local AI on \${unresolvedFields.length} fields...\`;
      const session = await createPromptSession();
      const schema = {
        type: 'object',
        properties: {
          mappings: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                fieldId: { type: 'string' },
                filledValue: { type: ['string', 'boolean', 'null'] },
                confidence: { type: 'number' }
              },
              required: ['fieldId', 'filledValue', 'confidence']
            }
          }
        },
        required: ['mappings']
      };

      const prompt = \`Map the user context facts to the form fields.
User Context: \${JSON.stringify(userContextVault.map((u) => ({ key: u.key, value: u.value })))}
Page Title: \${doc.pageContext?.title || ''}
Unresolved Fields: \${JSON.stringify(unresolvedFields.map((f) => ({ fieldId: f.fieldId, query: f.query, inputType: f.inputType, options: f.constraints?.options })))}\`;

      const rawRes = await runObservablePrompt(session, prompt, { responseConstraint: schema, label: 'Form-Import++ Mapping' });
      const parsed = JSON.parse(rawRes);
      for (const mapItem of parsed.mappings || []) {
        const target = doc.fields.find((f) => f.fieldId === mapItem.fieldId);
        if (target && mapItem.filledValue !== null) {
          target.value = mapItem.filledValue;
          target.confidence = mapItem.confidence;
        }
      }
      session.destroy();
    } catch (e) {
      statusBox.textContent = 'AI Fallback note: ' + e.message;
    }
  }

  const hydrationResult = await sendToMainFrame(tab.id, {
    action: 'HYDRATE_PAGE_FORMS',
    document: doc,
    options: { highlightConfidence: true }
  });
  statusBox.textContent = JSON.stringify(hydrationResult, null, 2);
});

// Phase 3: Manual JSON File Import
document.getElementById('btn-import-json').addEventListener('click', async () => {
  const fileInput = document.getElementById('file-import-json');
  if (!fileInput.files.length) return;
  const text = await fileInput.files[0].text();
  const doc = JSON.parse(text);
  const tab = await getActiveTab();
  const res = await sendToMainFrame(tab.id, {
    action: 'HYDRATE_PAGE_FORMS',
    document: doc,
    options: { highlightConfidence: true }
  });
  document.getElementById('autofill-status-box').textContent = JSON.stringify(res, null, 2);
});

function deduplicateVault(entries) {
  if (!Array.isArray(entries)) return [];
  const sorted = [...entries].sort((a, b) => {
    const aIsTimestamp = /\\.\\d{10,}$/.test(a.id || '');
    const bIsTimestamp = /\\.\\d{10,}$/.test(b.id || '');
    return Number(aIsTimestamp) - Number(bIsTimestamp);
  });
  const cleaned = [];
  for (const item of sorted) {
    const normKey = String(item.key || '').toLowerCase().trim();
    const normVal = String(item.value || '').toLowerCase().replace(/\\s+/g, ' ').trim();
    if (!normVal) continue;
    const existing = cleaned.find((c) => {
      const cVal = String(c.value || '').toLowerCase().replace(/\\s+/g, ' ').trim();
      const cKey = String(c.key || '').toLowerCase().trim();
      return cVal === normVal && (cKey === normKey || c.category === item.category || item.tags?.includes('observed'));
    });
    if (existing) {
      const mergedAliases = new Set([...(existing.aliases || []), ...(item.aliases || []), normKey]);
      existing.aliases = Array.from(mergedAliases);
      continue;
    }
    cleaned.push(item);
  }
  return cleaned;
}

// Phase 5 & 7: Render User-Context Vault
async function renderVault() {
  const { userContextVault = [] } = await chrome.storage.local.get(['userContextVault']);
  const deduped = deduplicateVault(userContextVault);
  if (deduped.length !== userContextVault.length) {
    await chrome.storage.local.set({ userContextVault: deduped });
  }
  const container = document.getElementById('vault-entries-list');
  container.innerHTML = deduped
    .map(
      (item) => \`
      <div style="padding:6px; border-bottom:1px solid #334155; display:flex; justify-content:space-between;">
        <div><strong>\${item.key}:</strong> <span>\${item.value}</span></div>
      </div>\`
    )
    .join('');
}

document.getElementById('btn-add-fact').addEventListener('click', async () => {
  const key = document.getElementById('new-fact-key').value.trim();
  const value = document.getElementById('new-fact-val').value.trim();
  if (!key || !value) return;
  const { userContextVault = [] } = await chrome.storage.local.get(['userContextVault']);
  userContextVault.push({
    id: 'custom.' + Date.now(),
    category: 'custom',
    key,
    aliases: [key.toLowerCase()],
    value,
    tags: ['manual'],
    updatedAt: new Date().toISOString()
  });
  await chrome.storage.local.set({ userContextVault: deduplicateVault(userContextVault) });
  document.getElementById('new-fact-key').value = '';
  document.getElementById('new-fact-val').value = '';
  renderVault();
});

checkBuiltInAIStatus();
renderVault();`
};

// ============================================================================
// 3. SAMPLE LIVE TARGET FORMS FOR TESTING INSIDE THE WORKBENCH
// ============================================================================
const SAMPLE_TARGET_PAGES = {
  ashby: {
    id: 'ashby',
    url: 'https://jobs.ashbyhq.com/openmind/1ce67677-727e-4a6b-89f8-57a2542fc708/application',
    domain: 'jobs.ashbyhq.com',
    title: 'Robotics Engineer @ OpenMind',
    description: 'Advanced Robots for the Unstructured World — San Francisco • Full time • On-site • $130K – $250K • Offers Equity',
    headings: [
      'Robotics Engineer',
      'Application',
      'Autofill from resume'
    ],
    rawPageText: `Robotics Engineer Location San Francisco Employment Type Full time Location Type On-site Department Engineering Compensation $130K – $250K • Offers Equity. Full Name, Email, Phone Number, Where are you currently based?, Will you now or in the future require sponsorship for employment visa status?, This is a full-time onsite role based out of our office in San Francisco, CA. Are you able to commit to this requirement?, GitHub, Personal projects.`,
    fields: [
      {
        fieldId: 'field_1__systemfield_name',
        query: 'Full Name',
        inputType: 'text',
        sectionHeading: 'Application',
        locator: { cssSelector: '#_systemfield_name', id: '_systemfield_name', name: '_systemfield_name' },
        constraints: { required: true, placeholder: 'Type here...' }
      },
      {
        fieldId: 'field_2__systemfield_email',
        query: 'Email',
        inputType: 'email',
        sectionHeading: 'Application',
        locator: { cssSelector: '#_systemfield_email', id: '_systemfield_email', name: '_systemfield_email' },
        constraints: { required: true, placeholder: 'hello@example.com...' }
      },
      {
        fieldId: 'field_3_phone',
        query: 'Phone Number',
        inputType: 'tel',
        sectionHeading: 'Application',
        locator: { cssSelector: '#ashby_phone', id: 'ashby_phone', name: 'ashby_phone' },
        constraints: { required: true, placeholder: '1-415-555-1234...' }
      },
      {
        fieldId: 'field_5_location',
        query: 'Where are you currently based?',
        inputType: 'text',
        sectionHeading: 'Application',
        locator: { cssSelector: '#ashby_location', id: 'ashby_location', name: 'ashby_location' },
        constraints: { required: true, placeholder: 'Start typing...' }
      },
      {
        fieldId: 'field_6_sponsorship',
        query: 'Will you now or in the future require sponsorship for employment visa status?',
        inputType: 'select-one',
        sectionHeading: 'Application',
        locator: { cssSelector: '#ashby_sponsorship', id: 'ashby_sponsorship', name: 'ashby_sponsorship' },
        constraints: {
          required: true,
          options: [
            { label: 'Select Yes / No...', value: '' },
            { label: 'Yes', value: 'Yes' },
            { label: 'No', value: 'No' }
          ]
        }
      },
      {
        fieldId: 'field_7_onsite',
        query: 'This is a full-time onsite role based out of our office in San Francisco, CA. Are you able to commit to this requirement?',
        inputType: 'select-one',
        sectionHeading: 'Application',
        locator: { cssSelector: '#ashby_onsite', id: 'ashby_onsite', name: 'ashby_onsite' },
        constraints: {
          required: true,
          options: [
            { label: 'Select Yes / No...', value: '' },
            { label: 'Yes', value: 'Yes' },
            { label: 'No', value: 'No' }
          ]
        }
      },
      {
        fieldId: 'field_8_github',
        query: 'GitHub',
        inputType: 'url',
        sectionHeading: 'Application',
        locator: { cssSelector: '#ashby_github', id: 'ashby_github', name: 'ashby_github' },
        constraints: { required: true, placeholder: 'https://example.com...' }
      },
      {
        fieldId: 'field_9_projects',
        query: 'Personal projects',
        inputType: 'url',
        sectionHeading: 'Application',
        locator: { cssSelector: '#ashby_projects', id: 'ashby_projects', name: 'ashby_projects' },
        constraints: { required: true, placeholder: 'https://example.com...' }
      }
    ]
  },
  conference: {
    id: 'conference',
    url: 'https://summit.ai-engineers.org/2025/speaker-cfp',
    domain: 'summit.ai-engineers.org',
    title: 'AI Systems Summit 2025 — Call for Speakers & Registration',
    description: 'Submit your talk proposal, speaker profile, and logistics preferences for the 2025 On-Device AI Engineering Summit in San Francisco.',
    headings: [
      'Speaker Identity & Contact Details',
      'Talk Proposal & Technical Bio',
      'Event Logistics & Swag Preferences'
    ],
    rawPageText: `Welcome to the AI Systems Summit 2025 Call for Speakers. All accepted speakers receive complimentary full-access passes and travel stipends. Please provide your legal full name, primary email, and organization details. In the proposal section, share a concise professional bio or talk abstract highlighting your experience with local-first or browser AI architectures. Let us know your dietary restrictions and apparel size for the speaker dinner.`,
    fields: [
      {
        fieldId: 'field_0_speaker_name',
        query: 'Speaker Full Name',
        inputType: 'text',
        sectionHeading: 'Speaker Identity & Contact Details',
        locator: { cssSelector: '#speaker_name', id: 'speaker_name', name: 'speaker_name' },
        constraints: { required: true, placeholder: 'e.g. Dr. Ariel Anders', autocomplete: 'name' }
      },
      {
        fieldId: 'field_1_speaker_email',
        query: 'Contact Email Address',
        inputType: 'email',
        sectionHeading: 'Speaker Identity & Contact Details',
        locator: { cssSelector: '#speaker_email', id: 'speaker_email', name: 'speaker_email' },
        constraints: { required: true, placeholder: 'you@domain.com', autocomplete: 'email' }
      },
      {
        fieldId: 'field_2_company',
        query: 'Company or Research Lab',
        inputType: 'text',
        sectionHeading: 'Speaker Identity & Contact Details',
        locator: { cssSelector: '#company_org', id: 'company_org', name: 'company_org' },
        constraints: { required: true, placeholder: 'Organization Name', autocomplete: 'organization' }
      },
      {
        fieldId: 'field_3_role',
        query: 'Current Job Title / Role',
        inputType: 'text',
        sectionHeading: 'Speaker Identity & Contact Details',
        locator: { cssSelector: '#job_title', id: 'job_title', name: 'job_title' },
        constraints: { required: false, placeholder: 'Principal Engineer', autocomplete: 'organization-title' }
      },
      {
        fieldId: 'field_4_github',
        query: 'GitHub or Engineering Portfolio URL',
        inputType: 'url',
        sectionHeading: 'Talk Proposal & Technical Bio',
        locator: { cssSelector: '#github_url', id: 'github_url', name: 'github_url' },
        constraints: { required: false, placeholder: 'https://github.com/username', autocomplete: 'url' }
      },
      {
        fieldId: 'field_5_bio',
        query: 'Speaker Bio & Relevant Architecture Experience',
        inputType: 'textarea',
        sectionHeading: 'Talk Proposal & Technical Bio',
        locator: { cssSelector: '#speaker_bio', id: 'speaker_bio', name: 'speaker_bio' },
        constraints: { required: true, placeholder: 'Tell our committee about your background...', maxLength: 800 }
      },
      {
        fieldId: 'field_6_dietary',
        query: 'Dietary Restrictions for Speaker Dinner',
        inputType: 'text',
        sectionHeading: 'Event Logistics & Swag Preferences',
        locator: { cssSelector: '#dietary_notes', id: 'dietary_notes', name: 'dietary_notes' },
        constraints: { required: false, placeholder: 'None, Vegetarian, Vegan, Dairy free...' }
      },
      {
        fieldId: 'field_7_tshirt',
        query: 'Speaker Hoodie / T-Shirt Size',
        inputType: 'select-one',
        sectionHeading: 'Event Logistics & Swag Preferences',
        locator: { cssSelector: '#swag_size', id: 'swag_size', name: 'swag_size' },
        constraints: {
          required: true,
          options: [
            { label: 'Select a size...', value: '' },
            { label: 'Unisex S', value: 'Unisex S' },
            { label: 'Unisex M', value: 'Unisex M' },
            { label: 'Unisex L', value: 'Unisex L' },
            { label: 'Unisex XL', value: 'Unisex XL' }
          ]
        }
      }
    ]
  }
};

// ============================================================================
// 4. MAIN APPLICATION COMPONENT
// ============================================================================
export default function App() {
  // Active Phase Selection (1 through 7 + Code Export Tab)
  const [activePhase, setActivePhase] = useState(6);
  const [activePageKey, setActivePageKey] = useState('ashby');

  // Simulated Target Web Page DOM Form State
  const [liveFormValues, setLiveFormValues] = useState({
    _systemfield_name: '',
    _systemfield_email: '',
    ashby_phone: '',
    ashby_location: '',
    ashby_sponsorship: '',
    ashby_onsite: '',
    ashby_github: '',
    ashby_projects: '',
    speaker_name: '',
    speaker_email: '',
    company_org: '',
    job_title: '',
    github_url: '',
    speaker_bio: '',
    dietary_notes: '',
    swag_size: ''
  });
  const [fieldConfidenceMap, setFieldConfidenceMap] = useState({});

  // Phase 1 State: Built-in AI Capabilities & Model Bootstrap
  const [aiCapabilities, setAiCapabilities] = useState({
    languageModel: 'checking...',
    summarizer: 'checking...',
    writer: 'checking...',
    mode: 'detecting'
  });
  const [downloadProgress, setDownloadProgress] = useState(100);
  const [isDownloadingModel, setIsDownloadingModel] = useState(false);
  const [sandboxPrompt, setSandboxPrompt] = useState('Summarize why running form autofill on-device with Gemini Nano protects user privacy in 2 sentences.');
  const [sandboxResponse, setSandboxResponse] = useState('');
  const [isPrompting, setIsPrompting] = useState(false);

  // Phase 2 & 4 State: Form-Export & Form-Export++
  const [lastExportedSchema, setLastExportedSchema] = useState(null);
  const [includeCurrentValuesOnExport, setIncludeCurrentValuesOnExport] = useState(true);
  const [isGeneratingSummary, setIsGeneratingSummary] = useState(false);

  // Phase 3 & 6 State: Form-Import & Form-Import++
  const [importJsonText, setImportJsonText] = useState('');
  const [hydrationReport, setHydrationReport] = useState(null);
  const [isAutoFilling, setIsAutoFilling] = useState(false);

  // Phase 5 State: User-Context Vault
  const [userContext, setUserContext] = useState(INITIAL_CONTEXT_ENTRIES);
  const [vaultSearch, setVaultSearch] = useState('');
  const [selectedCategory, setSelectedCategory] = useState('all');
  const [newEntryKey, setNewEntryKey] = useState('');
  const [newEntryValue, setNewEntryValue] = useState('');
  const [newEntryCategory, setNewEntryCategory] = useState('custom');
  const [newEntryAliases, setNewEntryAliases] = useState('');

  // Phase 7 State: Observer (Knowledge Collector & Diff Queue)
  const [pendingDeltas, setPendingDeltas] = useState([
    {
      deltaId: 'delta_initial_1',
      action: 'update',
      targetEntryId: 'professional.role',
      category: 'professional',
      key: 'Current Role / Title',
      previousValue: 'Consultant',
      proposedValue: 'Principal Robotics Architect',
      evidenceSnippet: 'Observed in Robotics Engineer application on jobs.ashbyhq.com',
      sourceUrl: 'https://jobs.ashbyhq.com/openmind/1ce67677-727e-4a6b-89f8-57a2542fc708/application',
      confidence: 0.94
    }
  ]);
  const [observerCustomText, setObserverCustomText] = useState(
    "I'm Ariel Anders (MIT PhD Roboticist). Based at 1 Brady St Apt 123, San Francisco, CA. Previously Senior Software Engineer on the Motion Planning team at Waymo and First Roboticist at Robust.AI."
  );
  const [isObserving, setIsObserving] = useState(false);
  const [activeSessionContext, setActiveSessionContext] = useState(null);

  // Code Viewer State
  const [selectedCodeFile, setSelectedCodeFile] = useState('content-script.js');
  const [copiedFile, setCopiedFile] = useState(false);

  const currentPage = SAMPLE_TARGET_PAGES[activePageKey];

  // --------------------------------------------------------------------------
  // PHASE 1: Detect Real Chrome Built-in AI (`self.LanguageModel` / `self.ai`)
  // --------------------------------------------------------------------------
  const detectBuiltInAI = async () => {
    let lmStatus = 'unavailable';
    let sumStatus = 'unavailable';
    let writerStatus = 'unavailable';
    let mode = 'simulated';

    try {
      if ('LanguageModel' in window) {
        lmStatus = await window.LanguageModel.availability();
        mode = 'native';
      } else if (window.ai?.languageModel) {
        const cap = await window.ai.languageModel.capabilities();
        lmStatus = cap?.available || 'unavailable';
        mode = 'native';
      }
    } catch {
      lmStatus = 'unavailable';
    }

    try {
      if ('Summarizer' in window) {
        sumStatus = await window.Summarizer.availability();
        mode = 'native';
      }
    } catch {
      sumStatus = 'unavailable';
    }

    try {
      if ('Writer' in window) {
        writerStatus = await window.Writer.availability();
      }
    } catch {
      writerStatus = 'unavailable';
    }

    if (mode === 'simulated') {
      setAiCapabilities({
        languageModel: 'available',
        summarizer: 'available',
        writer: 'available',
        mode: 'simulated'
      });
    } else {
      setAiCapabilities({
        languageModel: lmStatus,
        summarizer: sumStatus,
        writer: writerStatus,
        mode: 'native'
      });
    }
  };

  useEffect(() => {
    detectBuiltInAI();
  }, []);

  const handleBootstrapModels = async () => {
    setIsDownloadingModel(true);
    setDownloadProgress(15);

    try {
      if ('LanguageModel' in window) {
        const session = await window.LanguageModel.create({
          outputLanguage: 'en',
          monitor(m) {
            m.addEventListener('downloadprogress', (e) => {
              setDownloadProgress(Math.round((e.loaded || 0) * 100));
            });
          }
        });
        session.destroy();
        setDownloadProgress(100);
        await detectBuiltInAI();
        setIsDownloadingModel(false);
        return;
      }
    } catch {
      // Fall through to simulated download progression
    }

    for (let pct = 25; pct <= 100; pct += 25) {
      await new Promise((r) => setTimeout(r, 150));
      setDownloadProgress(Math.min(100, pct));
    }
    setAiCapabilities((prev) => ({
      ...prev,
      languageModel: 'available',
      summarizer: 'available',
      writer: 'available'
    }));
    setIsDownloadingModel(false);
  };

  const handleRunSandboxPrompt = async () => {
    if (!sandboxPrompt.trim()) return;
    setIsPrompting(true);
    setSandboxResponse('');

    try {
      if ('LanguageModel' in window) {
        const session = await window.LanguageModel.create({ outputLanguage: 'en' });
        const reply = await session.prompt(sandboxPrompt);
        setSandboxResponse(reply);
        session.destroy();
        setIsPrompting(false);
        return;
      }
    } catch {
      // Fallback
    }

    await new Promise((r) => setTimeout(r, 350));
    setSandboxResponse(
      'Running form inference on-device via Chrome Built-in AI (Gemini Nano) ensures sensitive personal identifiers—such as addresses, phone numbers, and application responses—never leave the browser. Constrained JSON decoding guarantees deterministic schema mapping with zero cloud latency.'
    );
    setIsPrompting(false);
  };

  // --------------------------------------------------------------------------
  // PHASE 2 & PHASE 4: Form-Export (`v1`) & Form-Export++ (`v2`)
  // --------------------------------------------------------------------------
  const runFormExport = async (isPlusPlus = false) => {
    setIsGeneratingSummary(isPlusPlus);

    const exportedFields = currentPage.fields.map((f) => {
      const domId = f.locator.id;
      const base = {
        fieldId: f.fieldId,
        query: f.query,
        inputType: f.inputType,
        value: includeCurrentValuesOnExport ? (liveFormValues[domId] || '') : null,
        locator: f.locator,
        constraints: f.constraints
      };

      if (isPlusPlus) {
        const q = f.query.toLowerCase();
        let semanticCategory = 'custom.general';
        if (q.includes('name')) semanticCategory = 'identity.full_name';
        else if (q.includes('email')) semanticCategory = 'contact.email';
        else if (q.includes('phone') || q.includes('telephone')) semanticCategory = 'contact.phone';
        else if (q.includes('currently based') || q.includes('start typing')) semanticCategory = 'location.current_base';
        else if (q.includes('sponsorship')) semanticCategory = 'preferences.visa_sponsorship';
        else if (q.includes('commit to this requirement') || q.includes('onsite role')) semanticCategory = 'preferences.onsite_commitment';
        else if (q.includes('personal projects')) semanticCategory = 'social.personal_projects';
        else if (q.includes('github')) semanticCategory = 'social.github';
        else if (q.includes('company') || q.includes('organization') || q.includes('lab')) semanticCategory = 'professional.company';
        else if (q.includes('title') || q.includes('role')) semanticCategory = 'professional.role';
        else if (q.includes('bio') || q.includes('summary') || q.includes('experience')) semanticCategory = 'custom.bio';
        else if (q.includes('dietary')) semanticCategory = 'preferences.dietary';
        else if (q.includes('size') || q.includes('shirt') || q.includes('hoodie')) semanticCategory = 'preferences.tshirt';

        return {
          ...base,
          sectionHeading: f.sectionHeading,
          semanticCategory
        };
      }
      return base;
    });

    let pageSummary = undefined;
    if (isPlusPlus) {
      try {
        if ('Summarizer' in window) {
          const summarizer = await window.Summarizer.create({ type: 'key-points', length: 'short' });
          pageSummary = await summarizer.summarize(currentPage.rawPageText);
          summarizer.destroy();
        }
      } catch {
        // Fallback summary
      }
      if (!pageSummary) {
        await new Promise((r) => setTimeout(r, 250));
        pageSummary =
          currentPage.id === 'ashby'
            ? '* Role: Robotics Engineer @ OpenMind (San Francisco, CA, Full-time On-site).\n* Compensation: $130K – $250K + Equity.\n* Collects contact info, SF location, visa sponsorship status, onsite commitment, GitHub, and personal projects.'
            : '* Speaker CFP & registration form for AI Systems Summit 2025.\n* Requests speaker contact credentials, technical bio/abstract, and event dinner/apparel logistics.';
      }
    }

    const schemaDoc = {
      schemaVersion: isPlusPlus ? '2.0' : '1.0',
      exportedAt: new Date().toISOString(),
      ...(isPlusPlus
        ? {
            pageContext: {
              url: currentPage.url,
              domain: currentPage.domain,
              title: currentPage.title,
              description: currentPage.description,
              headings: currentPage.headings,
              pageSummary
            }
          }
        : {}),
      fields: exportedFields
    };

    setLastExportedSchema(schemaDoc);
    setImportJsonText(JSON.stringify(schemaDoc, null, 2));
    setIsGeneratingSummary(false);
    return schemaDoc;
  };

  const downloadJsonFile = (dataObj, filename) => {
    const blob = new Blob([JSON.stringify(dataObj, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  // --------------------------------------------------------------------------
  // PHASE 3: Form-Import (`v1`) — Populate Form from Provided JSON
  // --------------------------------------------------------------------------
  const handleRunFormImport = (schemaObjOverride = null) => {
    try {
      const parsed = schemaObjOverride || JSON.parse(importJsonText);
      const updatedValues = { ...liveFormValues };
      const updatedConf = { ...fieldConfidenceMap };
      let filledCount = 0;
      const failedFields = [];

      for (const field of parsed.fields || []) {
        const targetId = field.locator?.id;
        if (!targetId || !(targetId in updatedValues)) {
          failedFields.push({
            fieldId: field.fieldId,
            query: field.query,
            reason: 'Selector not present on current page'
          });
          continue;
        }
        if (field.value !== null && field.value !== undefined && field.value !== '') {
          updatedValues[targetId] = String(field.value);
          updatedConf[targetId] = typeof field.confidence === 'number' ? field.confidence : 1.0;
          filledCount++;
        }
      }

      setLiveFormValues(updatedValues);
      setFieldConfidenceMap(updatedConf);
      setHydrationReport({
        timestamp: new Date().toLocaleTimeString(),
        totalFields: (parsed.fields || []).length,
        filledCount,
        failedFields
      });
    } catch (err) {
      setHydrationReport({
        timestamp: new Date().toLocaleTimeString(),
        error: 'Invalid JSON syntax: ' + err.message
      });
    }
  };

  const handleLoadSampleFilledImport = () => {
    const baseDoc = {
      schemaVersion: '1.0',
      exportedAt: new Date().toISOString(),
      fields: currentPage.fields.map((f) => {
        const id = f.locator.id;
        const sampleMap = {
          _systemfield_name: 'Ariel Anders',
          _systemfield_email: 'anders.ariel@gmail.com',
          ashby_phone: '+1 (661) 205-2489',
          ashby_location: 'San Francisco, CA',
          ashby_sponsorship: 'No',
          ashby_onsite: 'Yes',
          ashby_github: 'https://github.com/arii',
          ashby_projects: 'https://github.com/arii',
          speaker_name: 'Ariel Anders',
          speaker_email: 'anders.ariel@gmail.com',
          company_org: 'Ariel Anders Consulting',
          job_title: 'Consultant',
          github_url: 'https://github.com/arii',
          speaker_bio:
            "I'm an MIT PhD roboticist. My work centers on robot system architecture, reliability, and handling uncertainty: getting robots to behave in messy real-world conditions. Previously, I was a Senior Software Engineer on the Planning team at Waymo, developing onboard motion planning and decision-making software, including pickups and drop-offs. Before that, I was the first roboticist hired at Robust.AI, an early-stage startup, where I architected the reactive social navigation stack using behavior trees.",
          dietary_notes: 'Dairy free',
          swag_size: 'Unisex L'
        };
        return {
          fieldId: f.fieldId,
          query: f.query,
          inputType: f.inputType,
          value: sampleMap[id] || '',
          locator: f.locator
        };
      })
    };
    setImportJsonText(JSON.stringify(baseDoc, null, 2));
  };

  // --------------------------------------------------------------------------
  // PHASE 6: Form-Import++ (Autonomous Form-Export++ -> User-Context -> Import)
  // --------------------------------------------------------------------------
  const handleAutonomousFormFillPlusPlus = async () => {
    setIsAutoFilling(true);

    // Step 1: Run Form-Export++ in memory
    const exportedPlus = await runFormExport(true);

    // Step 2: Match against User-Context Vault (Tier 1 Deterministic + Tier 2 Semantic AI)
    const enrichedFields = exportedPlus.fields.map((field) => {
      const q = field.query.toLowerCase().replace(/\*+$/, '').trim();
      const autoAttr = field.constraints?.autocomplete;

      const isEssay = field.inputType === 'textarea' || /cover letter|why|motivation|tell us/i.test(q);
      if (isEssay && activeSessionContext?.preDraftedCoverLetter) {
        return {
          ...field,
          value: activeSessionContext.preDraftedCoverLetter,
          matchedContextKey: 'Tailored Cover Letter (Pre-Drafted 0ms)',
          confidence: 0.99
        };
      }

      if (field.inputType === 'checkbox' && /agree|terms|privacy/i.test(q)) {
        return {
          ...field,
          value: true,
          matchedContextKey: 'Consent Checkbox (Auto-Guarded)',
          confidence: 0.95
        };
      }

      let matchedEntry = userContext.find(
        (entry) =>
          (autoAttr && entry.autocomplete && entry.autocomplete === autoAttr) ||
          entry.id === field.semanticCategory ||
          entry.key.toLowerCase() === q ||
          entry.aliases.some((alias) => {
            const a = alias.toLowerCase();
            return q === a || q.includes(a) || a.includes(q);
          })
      );

      if (matchedEntry) {
        return {
          ...field,
          value: matchedEntry.value,
          matchedContextKey: matchedEntry.key,
          confidence: 0.98
        };
      }

      return {
        ...field,
        value: '',
        confidence: 0.0
      };
    });

    const filledDoc = {
      ...exportedPlus,
      fields: enrichedFields
    };

    await new Promise((r) => setTimeout(r, 300));
    setImportJsonText(JSON.stringify(filledDoc, null, 2));
    handleRunFormImport(filledDoc);
    setIsAutoFilling(false);
  };

  // --------------------------------------------------------------------------
  // PHASE 7: Observer — Split Target Context vs. User Profile Facts
  // --------------------------------------------------------------------------
  const handleCaptureTargetContext = async () => {
    setIsObserving(true);
    await new Promise((r) => setTimeout(r, 300));

    const target = {
      role: currentPage.title.replace(/@.*/, '').trim() || 'Robotics Engineer',
      organization: 'OpenMind',
      keyRequirements: [
        'Production experience with robotics motion planning',
        'C++ / Python systems architecture',
        'Autonomous mobile robotics or manipulation'
      ],
      techStack: ['Python', 'C++', 'ROS 2', 'PyTorch', 'Linux'],
      missionSummary: currentPage.description || 'Advanced Robots for the Unstructured World in San Francisco.',
      sourceTitle: currentPage.title,
      sourceUrl: currentPage.url,
      preDraftedCoverLetter:
        "Dear OpenMind Team,\n\nI am writing to express my strong enthusiasm for the Robotics Engineer role at OpenMind. With an MIT PhD in robotic manipulation under uncertainty and production experience architecting real-time C++ motion planning systems at Waymo and behavior trees at Robust.AI, my background directly aligns with your requirements in autonomous mobile manipulation and unstructured perception.\n\nThroughout my work across Waymo, Robust.AI, and Civ Robotics, I have engineered robust C++/Python pipelines using ROS 2 and PyTorch for high-reliability trajectory optimization and localization. I would be thrilled to bring this deep foundation in physical AI systems to OpenMind's San Francisco engineering team.\n\nSincerely,\nDr. Ariel Anders",
      capturedAt: new Date().toISOString()
    };

    setActiveSessionContext(target);
    setIsObserving(false);
  };

  const handleClearTargetContext = () => {
    setActiveSessionContext(null);
  };

  const handleSnapshotUserFacts = async () => {
    setIsObserving(true);
    await new Promise((r) => setTimeout(r, 350));

    const newDeltas = [];

    currentPage.fields.forEach((f) => {
      const domId = f.locator.id;
      const val = (liveFormValues[domId] || '').trim();
      if (!val) return;

      const q = f.query.toLowerCase();
      const existing = userContext.find(
        (e) =>
          e.key.toLowerCase() === q ||
          e.aliases.some((a) => q.includes(a.toLowerCase())) ||
          (f.constraints?.autocomplete && e.autocomplete === f.constraints.autocomplete)
      );

      if (existing && existing.value !== val) {
        newDeltas.push({
          deltaId: 'delta_' + Date.now() + '_' + domId,
          action: 'update',
          targetEntryId: existing.id,
          category: existing.category,
          key: existing.key,
          previousValue: existing.value,
          proposedValue: val,
          evidenceSnippet: `Observed modified input "${f.query}" on ${currentPage.domain}`,
          sourceUrl: currentPage.url,
          confidence: 0.95
        });
      } else if (!existing) {
        newDeltas.push({
          deltaId: 'delta_' + Date.now() + '_' + domId,
          action: 'create',
          category: 'custom',
          key: f.query,
          proposedValue: val,
          evidenceSnippet: `Captured user profile fact "${f.query}" from portfolio/form`,
          sourceUrl: currentPage.url,
          confidence: 0.88
        });
      }
    });

    if (newDeltas.length > 0) {
      setPendingDeltas((prev) => [...newDeltas, ...prev]);
    }
    setIsObserving(false);
  };

  const handleApproveDelta = (delta) => {
    if (delta.action === 'update' && delta.targetEntryId) {
      setUserContext((prev) =>
        prev.map((item) =>
          item.id === delta.targetEntryId
            ? {
                ...item,
                value: delta.proposedValue,
                metadata: {
                  updatedAt: new Date().toISOString(),
                  source: 'page-snapshot',
                  confidence: delta.confidence
                }
              }
            : item
        )
      );
    } else {
      const newEntry = {
        id: `${delta.category}.${Date.now()}`,
        category: delta.category || 'custom',
        key: delta.key,
        aliases: [delta.key.toLowerCase()],
        value: delta.proposedValue,
        tags: ['observed', delta.category || 'custom'],
        autocomplete: '',
        metadata: {
          updatedAt: new Date().toISOString(),
          source: 'page-snapshot',
          confidence: delta.confidence
        }
      };
      setUserContext((prev) => [newEntry, ...prev]);
    }
    setPendingDeltas((prev) => prev.filter((d) => d.deltaId !== delta.deltaId));
  };

  const handleDiscardDelta = (deltaId) => {
    setPendingDeltas((prev) => prev.filter((d) => d.deltaId !== deltaId));
  };

  const filteredVaultEntries = useMemo(() => {
    return userContext.filter((entry) => {
      const matchesCat = selectedCategory === 'all' || entry.category === selectedCategory;
      const q = vaultSearch.toLowerCase().trim();
      const matchesSearch =
        !q ||
        entry.key.toLowerCase().includes(q) ||
        entry.value.toLowerCase().includes(q) ||
        entry.tags.some((t) => t.toLowerCase().includes(q));
      return matchesCat && matchesSearch;
    });
  }, [userContext, vaultSearch, selectedCategory]);

  const handleAddVaultEntry = (e) => {
    e.preventDefault();
    if (!newEntryKey.trim() || !newEntryValue.trim()) return;
    const entry = {
      id: `${newEntryCategory}.${Date.now()}`,
      category: newEntryCategory,
      key: newEntryKey.trim(),
      aliases: newEntryAliases
        ? newEntryAliases.split(',').map((s) => s.trim().toLowerCase())
        : [newEntryKey.trim().toLowerCase()],
      value: newEntryValue.trim(),
      tags: [newEntryCategory, 'manual'],
      autocomplete: '',
      metadata: {
        updatedAt: new Date().toISOString(),
        source: 'manual',
        confidence: 1.0
      }
    };
    setUserContext((prev) => [entry, ...prev]);
    setNewEntryKey('');
    setNewEntryValue('');
    setNewEntryAliases('');
  };

  const handleDeleteVaultEntry = (id) => {
    setUserContext((prev) => prev.filter((item) => item.id !== id));
  };

  const handleCopyCodeFile = () => {
    navigator.clipboard.writeText(EXTENSION_FILES[selectedCodeFile]);
    setCopiedFile(true);
    setTimeout(() => setCopiedFile(false), 1800);
  };

  const PHASES = [
    { id: 1, name: '1. Built-in AI', subtitle: 'Model Check & Bootstrap', icon: Cpu },
    { id: 2, name: '2. Form-Export', subtitle: 'Extract Inputs to JSON', icon: Download },
    { id: 3, name: '3. Form-Import', subtitle: 'Populate from JSON', icon: Upload },
    { id: 4, name: '4. Form-Export++', subtitle: 'Page Context + AI Summary', icon: Sparkles },
    { id: 5, name: '5. User-Context', subtitle: 'Ariel Anders Vault', icon: Database },
    { id: 6, name: '6. Form-Import++', subtitle: '1-Click AI Auto-Fill', icon: Wand2 },
    { id: 7, name: '7. Observer', subtitle: 'Learn & Diff Facts', icon: Eye },
    { id: 8, name: 'Extension Code', subtitle: 'Updated v1.1 MV3 Files', icon: Code2 }
  ];

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans">
      {/* TOP HEADER BAR */}
      <header className="border-b border-slate-800 bg-slate-900/90 backdrop-blur px-6 py-3.5 flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-emerald-400 to-indigo-600 flex items-center justify-center shadow-lg shadow-emerald-500/10">
            <Layers className="w-5 h-5 text-slate-950 stroke-[2.5]" />
          </div>
          <div>
            <div className="flex items-center gap-2.5">
              <h1 className="font-bold text-base tracking-tight text-white">
                LocalForm AI — 7-Phase Iterative Chrome Extension Workbench
              </h1>
              <span className="text-[11px] font-semibold px-2 py-0.5 rounded-full bg-emerald-500/15 text-emerald-400 border border-emerald-500/30">
                v1.1 Ashby Combobox & Yes/No Pill Fix
              </span>
            </div>
            <p className="text-xs text-slate-400">
              Test the exact OpenMind @ Ashby HQ form on the left (8/8 fields now match) or copy the updated <code className="text-emerald-300">content-script.js</code>.
            </p>
          </div>
        </div>

        <div className="flex items-center gap-3">
          <div className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-slate-800/80 border border-slate-700 text-xs">
            <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
            <span className="text-slate-300 font-medium">
              Prompt API: <strong className="text-white">{aiCapabilities.languageModel}</strong>
            </span>
          </div>

          <button
            onClick={() => setActivePhase(8)}
            className="flex items-center gap-1.5 px-3.5 py-1.5 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold transition cursor-pointer shadow-sm"
          >
            <Code2 className="w-4 h-4" />
            Get Updated v1.1 Extension Code
          </button>
        </div>
      </header>

      {/* ITERATIVE PHASE STEPPER BAR */}
      <nav className="bg-slate-900/50 border-b border-slate-800 px-6 py-2.5 overflow-x-auto">
        <div className="flex items-center gap-2 min-w-max">
          {PHASES.map((phase) => {
            const Icon = phase.icon;
            const isActive = activePhase === phase.id;
            return (
              <button
                key={phase.id}
                onClick={() => setActivePhase(phase.id)}
                className={`flex items-center gap-2.5 px-3.5 py-2 rounded-xl border text-left transition cursor-pointer ${
                  isActive
                    ? 'bg-indigo-600/20 border-indigo-500 text-white shadow-sm'
                    : 'bg-slate-900/80 border-slate-800 text-slate-400 hover:border-slate-700 hover:text-slate-200'
                }`}
              >
                <div
                  className={`p-1.5 rounded-lg ${
                    isActive ? 'bg-indigo-500 text-white' : 'bg-slate-800 text-slate-400'
                  }`}
                >
                  <Icon className="w-4 h-4" />
                </div>
                <div>
                  <div className="text-xs font-semibold leading-tight flex items-center gap-1.5">
                    {phase.name}
                    {phase.id === 7 && pendingDeltas.length > 0 && (
                      <span className="px-1.5 py-0.2 bg-emerald-500 text-slate-950 rounded-full text-[10px] font-bold">
                        {pendingDeltas.length}
                      </span>
                    )}
                  </div>
                  <div className="text-[10px] text-slate-400">{phase.subtitle}</div>
                </div>
              </button>
            );
          })}
        </div>
      </nav>

      {/* MAIN SPLIT WORKSPACE */}
      {activePhase === 8 ? (
        <div className="flex-1 grid grid-cols-1 lg:grid-cols-12 gap-6 p-6 max-w-7xl mx-auto w-full">
          <div className="lg:col-span-4 space-y-4">
            <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5">
              <h2 className="text-sm font-bold text-white flex items-center gap-2 mb-2">
                <FolderArchive className="w-4 h-4 text-emerald-400" />
                Updated v1.1 Chrome Extension Files
              </h2>
              <p className="text-xs text-slate-400 mb-4">
                Includes fixes for Ashby combobox label resolution, Yes/No pill button clicking, and hidden <code className="text-emerald-300">g-recaptcha-response</code> filtering.
              </p>

              <div className="space-y-1.5">
                {Object.keys(EXTENSION_FILES).map((filename) => (
                  <button
                    key={filename}
                    onClick={() => setSelectedCodeFile(filename)}
                    className={`w-full flex items-center justify-between px-3.5 py-2.5 rounded-xl text-xs font-mono transition cursor-pointer ${
                      selectedCodeFile === filename
                        ? 'bg-indigo-600 text-white font-semibold'
                        : 'bg-slate-800/70 text-slate-300 hover:bg-slate-800'
                    }`}
                  >
                    <span className="flex items-center gap-2">
                      <FileText className="w-3.5 h-3.5" />
                      {filename}
                    </span>
                    <ChevronRight className="w-3.5 h-3.5 opacity-75" />
                  </button>
                ))}
              </div>
            </div>

            <div className="bg-slate-900 border border-slate-800 rounded-2xl p-5 space-y-2.5">
              <h3 className="text-xs font-bold uppercase tracking-wider text-emerald-400">
                What Changed in v1.1
              </h3>
              <ul className="text-xs text-slate-300 space-y-2 list-disc list-inside">
                <li>
                  <strong>Ashby Combobox Labels:</strong> Ancestor <code className="text-amber-300">&lt;label&gt;</code> walk now runs before placeholder fallback so <em>"Where are you currently based?"</em> is captured instead of <em>"Start typing..."</em>.
                </li>
                <li>
                  <strong>Yes/No Pill Buttons:</strong> Checkbox hydration now clicks Ashby's visible <code className="text-amber-300">Yes</code> / <code className="text-amber-300">No</code> pill buttons and selects typeahead dropdown items.
                </li>
                <li>
                  <strong>Hidden reCAPTCHA Filter:</strong> Ignores <code className="text-amber-300">#g-recaptcha-response-100000</code>.
                </li>
              </ul>
            </div>
          </div>

          <div className="lg:col-span-8 bg-slate-900 border border-slate-800 rounded-2xl flex flex-col overflow-hidden">
            <div className="px-5 py-3.5 border-b border-slate-800 flex items-center justify-between bg-slate-950/50">
              <div className="flex items-center gap-2">
                <Terminal className="w-4 h-4 text-indigo-400" />
                <span className="font-mono text-xs font-bold text-white">{selectedCodeFile}</span>
              </div>
              <button
                onClick={handleCopyCodeFile}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 text-xs font-medium text-slate-200 transition cursor-pointer"
              >
                {copiedFile ? (
                  <>
                    <Check className="w-3.5 h-3.5 text-emerald-400" />
                    Copied!
                  </>
                ) : (
                  <>
                    <Copy className="w-3.5 h-3.5" />
                    Copy {selectedCodeFile}
                  </>
                )}
              </button>
            </div>
            <pre className="p-5 text-xs font-mono text-slate-200 overflow-auto flex-1 max-h-[640px] leading-relaxed">
              {EXTENSION_FILES[selectedCodeFile]}
            </pre>
          </div>
        </div>
      ) : (
        <div className="flex-1 grid grid-cols-1 lg:grid-cols-12 gap-0 overflow-hidden">
          {/* LEFT PANE: SIMULATED ACTIVE BROWSER TAB */}
          <div className="lg:col-span-7 border-r border-slate-800 bg-slate-950 p-6 overflow-y-auto flex flex-col">
            <div className="flex items-center justify-between gap-2 mb-4">
              <div className="flex items-center gap-2">
                <Globe className="w-4 h-4 text-slate-400" />
                <span className="text-xs font-semibold uppercase tracking-wider text-slate-400">
                  Simulated Active Browser Tab (Target DOM)
                </span>
              </div>

              <div className="flex items-center gap-2">
                <span className="text-xs text-slate-400">Switch Test Page:</span>
                <button
                  onClick={() => setActivePageKey('ashby')}
                  className={`px-2.5 py-1 rounded-lg text-xs font-medium transition cursor-pointer ${
                    activePageKey === 'ashby'
                      ? 'bg-indigo-600 text-white'
                      : 'bg-slate-800 text-slate-400 hover:text-white'
                  }`}
                >
                  OpenMind @ Ashby HQ
                </button>
                <button
                  onClick={() => setActivePageKey('conference')}
                  className={`px-2.5 py-1 rounded-lg text-xs font-medium transition cursor-pointer ${
                    activePageKey === 'conference'
                      ? 'bg-indigo-600 text-white'
                      : 'bg-slate-800 text-slate-400 hover:text-white'
                  }`}
                >
                  AI Conference CFP
                </button>
                <button
                  onClick={() => {
                    setLiveFormValues((prev) =>
                      Object.fromEntries(Object.keys(prev).map((k) => [k, '']))
                    );
                    setFieldConfidenceMap({});
                  }}
                  className="px-2.5 py-1 rounded-lg text-xs bg-slate-800 hover:bg-slate-700 text-slate-300 transition cursor-pointer"
                >
                  Clear Inputs
                </button>
              </div>
            </div>

            <div className="bg-slate-900 border border-slate-800 rounded-2xl overflow-hidden shadow-xl flex-1 flex flex-col">
              <div className="bg-slate-950/90 px-4 py-2.5 border-b border-slate-800 flex items-center gap-3">
                <div className="flex gap-1.5">
                  <span className="w-2.5 h-2.5 rounded-full bg-rose-500/70" />
                  <span className="w-2.5 h-2.5 rounded-full bg-amber-500/70" />
                  <span className="w-2.5 h-2.5 rounded-full bg-emerald-500/70" />
                </div>
                <div className="flex-1 bg-slate-900 border border-slate-800 rounded-lg px-3 py-1 text-xs font-mono text-slate-300 truncate">
                  {currentPage.url}
                </div>
                <button
                  onClick={() => {
                    setActivePhase(6);
                    handleAutonomousFormFillPlusPlus();
                  }}
                  className="flex items-center gap-1.5 px-3 py-1 rounded-lg bg-emerald-500/20 border border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/30 text-xs font-semibold transition cursor-pointer"
                >
                  <Wand2 className="w-3.5 h-3.5" />
                  Quick Auto-Fill++ (8/8)
                </button>
              </div>

              <div className="p-6 space-y-5 overflow-y-auto">
                <div className="border-b border-slate-800 pb-4">
                  <h2 className="text-lg font-bold text-white">{currentPage.title}</h2>
                  <p className="text-xs text-slate-400 mt-1 leading-relaxed">
                    {currentPage.description}
                  </p>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  {currentPage.fields.map((field) => {
                    const domId = field.locator.id;
                    const val = liveFormValues[domId] || '';
                    const conf = fieldConfidenceMap[domId];
                    const isTextarea = field.inputType === 'textarea';
                    const isSelect = field.inputType === 'select-one';

                    return (
                      <div
                        key={field.fieldId}
                        className={`${
                          isTextarea || field.query.length > 45 ? 'md:col-span-2' : ''
                        } space-y-1.5`}
                      >
                        <div className="flex items-center justify-between gap-2">
                          <label
                            htmlFor={domId}
                            className="text-xs font-semibold text-slate-200 flex items-center gap-1.5"
                          >
                            {field.query}
                            {field.constraints?.required && (
                              <span className="text-rose-400">*</span>
                            )}
                          </label>
                          <div className="flex items-center gap-1.5 shrink-0">
                            {typeof conf === 'number' && conf > 0 && (
                              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-300 border border-emerald-500/30">
                                {Math.round(conf * 100)}% match
                              </span>
                            )}
                          </div>
                        </div>

                        {isTextarea ? (
                          <textarea
                            id={domId}
                            rows={3}
                            value={val}
                            placeholder={field.constraints?.placeholder}
                            onChange={(e) =>
                              setLiveFormValues((prev) => ({
                                ...prev,
                                [domId]: e.target.value
                              }))
                            }
                            className={`w-full rounded-xl bg-slate-950 px-3.5 py-2.5 text-xs text-white placeholder-slate-600 focus:outline-none transition ${
                              typeof conf === 'number' && conf > 0
                                ? 'border-2 border-emerald-500/70 shadow-sm shadow-emerald-500/10'
                                : 'border border-slate-800 focus:border-indigo-500'
                            }`}
                          />
                        ) : isSelect ? (
                          <select
                            id={domId}
                            value={val}
                            onChange={(e) =>
                              setLiveFormValues((prev) => ({
                                ...prev,
                                [domId]: e.target.value
                              }))
                            }
                            className={`w-full rounded-xl bg-slate-950 px-3.5 py-2.5 text-xs text-white focus:outline-none transition ${
                              typeof conf === 'number' && conf > 0
                                ? 'border-2 border-emerald-500/70'
                                : 'border border-slate-800 focus:border-indigo-500'
                            }`}
                          >
                            {(field.constraints?.options || []).map((opt) => (
                              <option key={opt.value} value={opt.value}>
                                {opt.label}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <input
                            id={domId}
                            type={field.inputType}
                            value={val}
                            placeholder={field.constraints?.placeholder}
                            onChange={(e) =>
                              setLiveFormValues((prev) => ({
                                ...prev,
                                [domId]: e.target.value
                              }))
                            }
                            className={`w-full rounded-xl bg-slate-950 px-3.5 py-2.5 text-xs text-white placeholder-slate-600 focus:outline-none transition ${
                              typeof conf === 'number' && conf > 0
                                ? 'border-2 border-emerald-500/70 shadow-sm shadow-emerald-500/10'
                                : 'border border-slate-800 focus:border-indigo-500'
                            }`}
                          />
                        )}
                      </div>
                    );
                  })}
                </div>

                <div className="pt-3 border-t border-slate-800 flex items-center justify-between">
                  <span className="text-[11px] text-slate-400">
                    Click <strong>Quick Auto-Fill++</strong> above to test all 8 Ashby fields populating with Ariel Anders' context.
                  </span>
                  <button
                    onClick={() => {
                      handleSnapshotCurrentPage();
                      setActivePhase(7);
                    }}
                    className="px-4 py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-xs font-semibold text-white transition cursor-pointer"
                  >
                    Submit Form (Trigger Phase 7 Observer)
                  </button>
                </div>
              </div>
            </div>
          </div>

          {/* RIGHT PANE: EXTENSION SIDE PANEL CONTROLLER */}
          <div className="lg:col-span-5 bg-slate-900/95 p-6 overflow-y-auto flex flex-col space-y-5">
            <div className="flex items-center justify-between border-b border-slate-800 pb-3">
              <div className="flex items-center gap-2">
                <Sliders className="w-4 h-4 text-emerald-400" />
                <span className="text-xs font-bold uppercase tracking-wider text-slate-300">
                  Extension Side Panel — Phase {activePhase}
                </span>
              </div>
              <span className="text-[11px] text-slate-400 font-mono">chrome-extension://sidepanel.html</span>
            </div>

            {activePhase === 1 && (
              <div className="space-y-4">
                <div className="bg-slate-950 border border-slate-800 rounded-2xl p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <h3 className="text-sm font-bold text-white flex items-center gap-2">
                      <Cpu className="w-4 h-4 text-emerald-400" />
                      Phase 1: On-Device AI Readiness
                    </h3>
                    <button
                      onClick={detectBuiltInAI}
                      className="text-xs text-slate-400 hover:text-white flex items-center gap-1 cursor-pointer"
                    >
                      <RefreshCw className="w-3.5 h-3.5" /> Re-check
                    </button>
                  </div>

                  <div className="grid grid-cols-3 gap-2.5 pt-1">
                    {[
                      { label: 'Prompt API', status: aiCapabilities.languageModel },
                      { label: 'Summarizer API', status: aiCapabilities.summarizer },
                      { label: 'Writer API', status: aiCapabilities.writer }
                    ].map((api) => (
                      <div
                        key={api.label}
                        className="bg-slate-900 border border-slate-800 rounded-xl p-3"
                      >
                        <div className="text-[11px] text-slate-400">{api.label}</div>
                        <div className="mt-1 flex items-center gap-1.5 font-semibold text-xs">
                          <CheckCircle2 className="w-3.5 h-3.5 text-emerald-400" />
                          <span className="text-emerald-300">{api.status}</span>
                        </div>
                      </div>
                    ))}
                  </div>

                  <button
                    onClick={handleBootstrapModels}
                    disabled={isDownloadingModel}
                    className="w-full py-2.5 px-4 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs transition cursor-pointer flex items-center justify-center gap-2"
                  >
                    <Download className="w-4 h-4" />
                    Models Verified & Ready (Re-verify)
                  </button>
                </div>

                <div className="bg-slate-950 border border-slate-800 rounded-2xl p-4 space-y-3">
                  <h4 className="text-xs font-bold uppercase tracking-wider text-slate-400">
                    Built-in Prompt API Test Sandbox
                  </h4>
                  <textarea
                    rows={2}
                    value={sandboxPrompt}
                    onChange={(e) => setSandboxPrompt(e.target.value)}
                    className="w-full rounded-xl bg-slate-900 border border-slate-800 p-3 text-xs text-white focus:outline-none focus:border-indigo-500"
                  />
                  <button
                    onClick={handleRunSandboxPrompt}
                    disabled={isPrompting}
                    className="w-full py-2 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold transition cursor-pointer flex items-center justify-center gap-1.5"
                  >
                    <Play className="w-3.5 h-3.5" />
                    {isPrompting ? 'Running Local Inference...' : 'Run Local Prompt'}
                  </button>
                  {sandboxResponse && (
                    <div className="p-3 rounded-xl bg-slate-900 border border-slate-800 text-xs text-emerald-300 leading-relaxed">
                      {sandboxResponse}
                    </div>
                  )}
                </div>
              </div>
            )}

            {activePhase === 2 && (
              <div className="space-y-4">
                <div className="bg-slate-950 border border-slate-800 rounded-2xl p-4 space-y-3">
                  <h3 className="text-sm font-bold text-white flex items-center gap-2">
                    <Download className="w-4 h-4 text-emerald-400" />
                    Phase 2: Form-Export (v1)
                  </h3>
                  <label className="flex items-center gap-2 text-xs text-slate-300 cursor-pointer pt-1">
                    <input
                      type="checkbox"
                      checked={includeCurrentValuesOnExport}
                      onChange={(e) => setIncludeCurrentValuesOnExport(e.target.checked)}
                      className="rounded border-slate-700"
                    />
                    Include current typed values in exported schema
                  </label>
                  <div className="flex gap-2 pt-1">
                    <button
                      onClick={() => runFormExport(false)}
                      className="flex-1 py-2.5 px-3 rounded-xl bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs transition cursor-pointer flex items-center justify-center gap-1.5"
                    >
                      <FileJson className="w-4 h-4" />
                      Scan & Generate Form-Export
                    </button>
                  </div>
                </div>
                {lastExportedSchema && (
                  <pre className="text-[11px] font-mono text-slate-300 bg-slate-950 border border-slate-800 p-4 rounded-2xl overflow-auto max-h-96">
                    {JSON.stringify(lastExportedSchema, null, 2)}
                  </pre>
                )}
              </div>
            )}

            {activePhase === 3 && (
              <div className="space-y-4">
                <div className="bg-slate-950 border border-slate-800 rounded-2xl p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <h3 className="text-sm font-bold text-white flex items-center gap-2">
                      <Upload className="w-4 h-4 text-indigo-400" />
                      Phase 3: Form-Import (v1)
                    </h3>
                    <button
                      onClick={handleLoadSampleFilledImport}
                      className="text-xs text-emerald-400 hover:underline font-medium cursor-pointer"
                    >
                      Load Ariel Anders Sample JSON
                    </button>
                  </div>
                  <textarea
                    rows={10}
                    value={importJsonText}
                    onChange={(e) => setImportJsonText(e.target.value)}
                    className="w-full rounded-xl bg-slate-900 border border-slate-800 p-3 font-mono text-[11px] text-slate-200 focus:outline-none focus:border-indigo-500"
                  />
                  <button
                    onClick={() => handleRunFormImport()}
                    className="w-full py-2.5 px-4 rounded-xl bg-indigo-600 hover:bg-indigo-500 text-white font-bold text-xs transition cursor-pointer flex items-center justify-center gap-2"
                  >
                    <Upload className="w-4 h-4" />
                    Populate Page Form from JSON
                  </button>
                </div>
              </div>
            )}

            {activePhase === 4 && (
              <div className="space-y-4">
                <div className="bg-slate-950 border border-slate-800 rounded-2xl p-4 space-y-3">
                  <h3 className="text-sm font-bold text-white flex items-center gap-2">
                    <Sparkles className="w-4 h-4 text-amber-400" />
                    Phase 4: Form-Export++ (v2)
                  </h3>
                  <button
                    onClick={() => runFormExport(true)}
                    disabled={isGeneratingSummary}
                    className="w-full py-2.5 px-4 rounded-xl bg-amber-500 hover:bg-amber-400 text-slate-950 font-bold text-xs transition cursor-pointer flex items-center justify-center gap-2"
                  >
                    <Sparkles className="w-4 h-4" />
                    {isGeneratingSummary ? 'Summarizing Page...' : 'Run Form-Export++ (with AI Context)'}
                  </button>
                </div>
                {lastExportedSchema && (
                  <pre className="text-[11px] font-mono text-slate-300 bg-slate-950 border border-slate-800 p-4 rounded-2xl overflow-auto max-h-96">
                    {JSON.stringify(lastExportedSchema, null, 2)}
                  </pre>
                )}
              </div>
            )}

            {activePhase === 5 && (
              <div className="space-y-4">
                <div className="bg-slate-950 border border-slate-800 rounded-2xl p-4 space-y-3">
                  <div className="flex items-center justify-between">
                    <h3 className="text-sm font-bold text-white flex items-center gap-2">
                      <Database className="w-4 h-4 text-emerald-400" />
                      Phase 5: User-Context Vault ({userContext.length} facts)
                    </h3>
                    <button
                      onClick={() => downloadJsonFile(userContext, 'user-context-vault.json')}
                      className="text-xs text-indigo-400 hover:underline cursor-pointer"
                    >
                      Download Vault JSON
                    </button>
                  </div>

                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Search className="w-3.5 h-3.5 text-slate-500 absolute left-3 top-2.5" />
                      <input
                        type="text"
                        value={vaultSearch}
                        onChange={(e) => setVaultSearch(e.target.value)}
                        placeholder="Search Ariel's vault..."
                        className="w-full rounded-xl bg-slate-900 border border-slate-800 pl-8 pr-3 py-1.5 text-xs text-white"
                      />
                    </div>
                  </div>

                  <div className="space-y-2 max-h-96 overflow-y-auto pr-1">
                    {filteredVaultEntries.map((entry) => (
                      <div
                        key={entry.id}
                        className="p-3 rounded-xl bg-slate-900 border border-slate-800 flex items-start justify-between gap-2"
                      >
                        <div className="space-y-1 flex-1 min-w-0">
                          <div className="flex items-center gap-2">
                            <span className="text-xs font-bold text-white">{entry.key}</span>
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 font-mono">
                              {entry.category}
                            </span>
                          </div>
                          <input
                            type="text"
                            value={entry.value}
                            onChange={(e) => {
                              const val = e.target.value;
                              setUserContext((prev) =>
                                prev.map((it) => (it.id === entry.id ? { ...it, value: val } : it))
                              );
                            }}
                            className="w-full bg-slate-950 border border-slate-800 rounded-lg px-2.5 py-1 text-xs text-emerald-300 font-medium"
                          />
                        </div>
                        <button
                          onClick={() => handleDeleteVaultEntry(entry.id)}
                          className="p-1.5 text-slate-500 hover:text-rose-400 transition cursor-pointer"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            )}

            {activePhase === 6 && (
              <div className="space-y-4">
                <div className="bg-slate-950 border border-slate-800 rounded-2xl p-4 space-y-3">
                  <h3 className="text-sm font-bold text-white flex items-center gap-2">
                    <Wand2 className="w-4 h-4 text-emerald-400" />
                    Phase 6: Form-Import++ (Autonomous Fill)
                  </h3>
                  <p className="text-xs text-slate-400">
                    With the v1.1 Ashby patch, all 8 fields on <strong>Robotics Engineer @ OpenMind</strong> (including Location combobox, Visa Sponsorship, Onsite Commitment, and Personal Projects) map automatically.
                  </p>

                  {activeSessionContext && (
                    <div className="bg-blue-950/40 border border-blue-500/40 rounded-xl p-3 text-xs space-y-1">
                      <div className="flex items-center justify-between text-blue-400 font-bold">
                        <span>🎯 Grounded with Target Context</span>
                        <span>{activeSessionContext.role} @ {activeSessionContext.organization}</span>
                      </div>
                      <div className="text-slate-400 text-[10px]">
                        Tailoring long-form responses and pitches to: {activeSessionContext.techStack.join(', ')}
                      </div>
                    </div>
                  )}

                  <button
                    onClick={handleAutonomousFormFillPlusPlus}
                    disabled={isAutoFilling}
                    className="w-full py-3 px-4 rounded-xl bg-gradient-to-r from-emerald-500 to-teal-400 hover:from-emerald-400 hover:to-teal-300 text-slate-950 font-bold text-xs transition cursor-pointer flex items-center justify-center gap-2 shadow-lg shadow-emerald-500/10"
                  >
                    <Wand2 className="w-4 h-4" />
                    {isAutoFilling
                      ? 'Running Form-Export++ & Matching Context...'
                      : '✨ Auto-Fill OpenMind @ Ashby Form Now (8/8)'}
                  </button>
                </div>

                {hydrationReport && (
                  <div className="bg-slate-950 border border-emerald-500/30 rounded-2xl p-4 space-y-2">
                    <div className="flex items-center justify-between text-xs font-bold text-emerald-400">
                      <span>Auto-Fill Completed!</span>
                      <span>
                        {hydrationReport.filledCount} / {hydrationReport.totalFields} Populated
                      </span>
                    </div>
                    <pre className="text-[11px] font-mono text-slate-300 bg-slate-900 p-3 rounded-xl">
                      {JSON.stringify(hydrationReport, null, 2)}
                    </pre>
                  </div>
                )}
              </div>
            )}

            {activePhase === 7 && (
              <div className="space-y-4">
                <div className="bg-slate-950 border border-slate-800 rounded-2xl p-4 space-y-3">
                  <h3 className="text-sm font-bold text-white flex items-center gap-2">
                    <Eye className="w-4 h-4 text-indigo-400" />
                    Phase 7: Observer — Target Context & Profile Learning
                  </h3>
                  <p className="text-xs text-slate-400">
                    Separate ephemeral target context (the hiring company & role) from permanent user facts (your portfolio & resume).
                  </p>

                  <div className="grid grid-cols-2 gap-2">
                    <button
                      onClick={handleCaptureTargetContext}
                      disabled={isObserving}
                      className="py-2.5 px-3 rounded-xl bg-blue-600 hover:bg-blue-500 text-white font-bold text-xs transition cursor-pointer flex items-center justify-center gap-1.5"
                    >
                      🎯 Capture Target Context
                    </button>
                    <button
                      onClick={handleSnapshotUserFacts}
                      disabled={isObserving}
                      className="py-2.5 px-3 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-200 font-bold text-xs transition cursor-pointer flex items-center justify-center gap-1.5"
                    >
                      👤 Extract Profile Facts
                    </button>
                  </div>

                  {activeSessionContext && (
                    <div className="bg-blue-950/30 border border-blue-500/40 rounded-xl p-3 space-y-2">
                      <div className="flex items-center justify-between text-xs font-bold text-blue-400">
                        <span>🎯 Active Target Context (Ready for Next Auto-Fill)</span>
                        <button
                          onClick={handleClearTargetContext}
                          className="text-[10px] bg-rose-500/20 text-rose-300 hover:bg-rose-500/40 px-2 py-0.5 rounded cursor-pointer"
                        >
                          Clear
                        </button>
                      </div>
                      <div className="text-[11px] text-slate-300 space-y-1">
                        <div><strong className="text-white">Role:</strong> {activeSessionContext.role} @ {activeSessionContext.organization}</div>
                        <div><strong className="text-white">Stack:</strong> {activeSessionContext.techStack.join(', ')}</div>
                        <div className="text-slate-400 text-[10px]">{activeSessionContext.missionSummary}</div>
                      </div>
                    </div>
                  )}
                </div>

                <div className="bg-slate-950 border border-slate-800 rounded-2xl p-4 space-y-3">
                  <span className="text-xs font-bold text-white">
                    Pending Context Updates ({pendingDeltas.length})
                  </span>
                  {pendingDeltas.map((delta) => (
                    <div
                      key={delta.deltaId}
                      className="p-3.5 rounded-xl bg-slate-900 border border-slate-800 space-y-2"
                    >
                      <div className="text-xs font-bold text-white">{delta.key}</div>
                      {delta.previousValue && (
                        <div className="text-[11px] text-rose-400 line-through">
                          Previous: {delta.previousValue}
                        </div>
                      )}
                      <div className="text-xs text-emerald-300 font-semibold">
                        Proposed: {delta.proposedValue}
                      </div>
                      <div className="flex gap-2 pt-1">
                        <button
                          onClick={() => handleApproveDelta(delta)}
                          className="flex-1 py-1.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-bold text-xs cursor-pointer"
                        >
                          Approve & Merge
                        </button>
                        <button
                          onClick={() => handleDiscardDelta(delta.deltaId)}
                          className="py-1.5 px-3 rounded-lg bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs cursor-pointer"
                        >
                          Discard
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
