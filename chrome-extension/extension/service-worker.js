// ============================================================================
// LocalForm AI - Background Service Worker (Manifest V3)
// ============================================================================

const SEED_CONTEXT_ENTRIES = [
  {
    id: "identity.full_name",
    category: "identity",
    key: "Legal Full Name",
    aliases: ["full name", "name", "your name", "applicant name", "speaker name", "first and last name"],
    value: "Ariel Anders",
    tags: ["personal", "identity", "core"],
    autocomplete: "name",
    updatedAt: "2026-11-10T01:42:00Z"
  },
  {
    id: "contact.email",
    category: "contact",
    key: "Primary Email",
    aliases: ["email", "email address", "work email", "contact email", "speaker email"],
    value: "anders.ariel@gmail.com",
    tags: ["contact", "work", "core"],
    autocomplete: "email",
    updatedAt: "2026-11-10T01:27:41.632Z"
  },
  {
    id: "contact.phone",
    category: "contact",
    key: "Phone Number",
    aliases: ["phone", "phone number", "mobile", "telephone", "cell phone", "contact number", "direct phone"],
    value: "+1 (661) 205-2489",
    tags: ["contact", "core"],
    autocomplete: "tel",
    updatedAt: "2026-11-10T01:26:34.938Z"
  },
  {
    id: "location.current_base",
    category: "location",
    key: "Current Location / Metro Area",
    aliases: ["where are you currently based", "current location", "location", "based in", "city and state", "start typing..."],
    value: "San Francisco, CA",
    tags: ["location", "core", "ashby"],
    autocomplete: "",
    updatedAt: "2026-11-10T01:42:00Z"
  },
  {
    id: "professional.role",
    category: "professional",
    key: "Current Role / Title",
    aliases: ["job title", "role", "position", "occupation", "current title", "title"],
    value: "Consultant",
    tags: ["work", "career"],
    autocomplete: "organization-title",
    updatedAt: "2026-11-10T01:26:43.272Z"
  },
  {
    id: "professional.company",
    category: "professional",
    key: "Company / Organization",
    aliases: ["company", "organization", "employer", "workplace", "company name", "research lab"],
    value: "Ariel Anders Consulting",
    tags: ["work", "career"],
    autocomplete: "organization",
    updatedAt: "2026-11-10T01:26:47.798Z"
  },
  {
    id: "professional.previous_companies",
    category: "professional",
    key: "Prior Robotics Experience (Waymo & Robust.AI)",
    aliases: ["previous company", "past employers", "work history", "prior experience"],
    value: "Waymo (Senior Software Engineer, Motion Planning for Pickups/Drop-offs); Robust.AI (First Roboticist hired, architected reactive social navigation stack using Behavior Trees)",
    tags: ["work", "career", "robotics"],
    autocomplete: "",
    updatedAt: "2026-11-10T01:42:00Z"
  },
  {
    id: "education.phd",
    category: "professional",
    key: "Education (MIT PhD & UC Santa Cruz BS)",
    aliases: ["education", "degree", "phd", "doctorate", "university", "alma mater"],
    value: "Ph.D. in Computer Science (Robotics), MIT; B.S. in Computer Engineering, UC Santa Cruz",
    tags: ["education", "mit", "robotics"],
    autocomplete: "",
    updatedAt: "2026-11-10T01:42:00Z"
  },
  {
    id: "location.address",
    category: "location",
    key: "Street Address",
    aliases: ["address", "street address", "mailing address", "shipping address"],
    value: "1 Brady St Apt 123",
    tags: ["location", "shipping"],
    autocomplete: "street-address",
    updatedAt: "2026-11-10T01:26:57.126Z"
  },
  {
    id: "location.city",
    category: "location",
    key: "City",
    aliases: ["city", "town", "locality"],
    value: "San Francisco",
    tags: ["location"],
    autocomplete: "address-level2",
    updatedAt: "2026-11-10T01:22:54.176Z"
  },
  {
    id: "social.github",
    category: "social",
    key: "GitHub Profile URL",
    aliases: ["github", "github url", "code repo", "github profile"],
    value: "https://github.com/arii",
    tags: ["social", "work", "links"],
    autocomplete: "url",
    updatedAt: "2026-11-10T01:27:01.340Z"
  },
  {
    id: "social.personal_projects",
    category: "social",
    key: "Personal Projects & Portfolio URL",
    aliases: ["personal projects", "projects", "portfolio", "website", "portfolio url", "personal website"],
    value: "https://github.com/arii",
    tags: ["social", "work", "links", "ashby"],
    autocomplete: "url",
    updatedAt: "2026-11-10T01:42:00Z"
  },
  {
    id: "preferences.visa_sponsorship",
    category: "preferences",
    key: "Require Visa Sponsorship",
    aliases: ["will you now or in the future require sponsorship", "require sponsorship for employment visa", "require sponsorship", "visa sponsorship"],
    value: "No",
    tags: ["career", "ashby", "compliance"],
    autocomplete: "",
    updatedAt: "2026-11-10T01:42:00Z"
  },
  {
    id: "preferences.onsite_commitment",
    category: "preferences",
    key: "Onsite San Francisco Commitment",
    aliases: ["full-time onsite role based out of our office in san francisco", "are you able to commit to this requirement", "commit to this requirement", "onsite role"],
    value: "Yes",
    tags: ["career", "preferences", "ashby"],
    autocomplete: "",
    updatedAt: "2026-11-10T01:42:00Z"
  },
  {
    id: "custom.bio",
    category: "custom",
    key: "Professional Bio & Summary",
    aliases: ["bio", "about you", "summary", "why should we pick you", "project description", "speaker bio", "qualifications", "cover letter"],
    value: "I'm an MIT PhD roboticist. My work centers on robot system architecture, reliability, and handling uncertainty: getting robots to behave in messy real-world conditions. Previously, I was a Senior Software Engineer on the Planning team at Waymo, developing onboard motion planning and decision-making software, including pickups and drop-offs. Before that, I was the first roboticist hired at Robust.AI, an early-stage startup, where I architected the reactive social navigation stack using behavior trees. I received a BS in Computer Engineering from UC Santa Cruz and my Doctorate in Computer Science from MIT, where my research focused on reliable robotic manipulation under uncertainty, with the vision of enabling household helpers. At MIT I also taught project-based collaborative robotics courses and built an iOS app for people with visual impairments. I'm currently considering full-time roles and consulting/project-based work in the Bay Area.",
    tags: ["bio", "conference", "grant"],
    autocomplete: "",
    updatedAt: "2026-11-10T01:27:32.364Z"
  },
  {
    id: "preferences.dietary",
    category: "preferences",
    key: "Dietary Restrictions",
    aliases: ["dietary", "food allergies", "meal preference", "dietary restrictions", "dietary notes"],
    value: "Dairy free",
    tags: ["events", "preferences"],
    autocomplete: "",
    updatedAt: "2026-11-10T01:27:07.345Z"
  },
  {
    id: "preferences.tshirt",
    category: "preferences",
    key: "Apparel / T-Shirt Size",
    aliases: ["t-shirt size", "shirt size", "swag size", "apparel", "hoodie size"],
    value: "Unisex L",
    tags: ["events", "preferences"],
    autocomplete: "",
    updatedAt: "2026-11-10T01:22:54.176Z"
  }
];

// Open side panel on toolbar action click
chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((err) => console.error("SidePanel setPanelBehavior error:", err));

chrome.runtime.onInstalled.addListener(async () => {
  const existing = await chrome.storage.local.get(["userContextVault", "pendingDeltas"]);
  if (!existing.userContextVault || existing.userContextVault.length === 0) {
    await chrome.storage.local.set({
      userContextVault: SEED_CONTEXT_ENTRIES,
      pendingDeltas: []
    });
  }
  updateBadge((existing.pendingDeltas || []).length);
});

// Update toolbar badge to reflect pending deltas count
function updateBadge(count) {
  if (count > 0) {
    chrome.action.setBadgeText({ text: String(count) });
    chrome.action.setBadgeBackgroundColor({ color: "#10b981" });
  } else {
    chrome.action.setBadgeText({ text: "" });
  }
}

// Handle messages from content script or sidepanel
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === "FORM_SUBMIT_DETECTED") {
    handleFormSubmitObservation(message.payload, sender.tab)
      .then((res) => sendResponse(res))
      .catch((err) => sendResponse({ error: err.message }));
    return true; // Keep message channel open for async response
  }

  if (message.action === "UPDATE_BADGE_COUNT") {
    updateBadge(message.count || 0);
    sendResponse({ ok: true });
    return false;
  }
});

async function handleFormSubmitObservation(payload, tab) {
  const { userContextVault = [], pendingDeltas = [] } = await chrome.storage.local.get([
    "userContextVault",
    "pendingDeltas"
  ]);

  const newDeltas = [];
  const sensitiveRegex = /password|passcode|cvv|cvc|cc-number|card-number|ssn|social-security/i;

  for (const field of payload.filledFormFields || []) {
    if (!field.value || typeof field.value !== "string") continue;
    if (sensitiveRegex.test(field.query) || sensitiveRegex.test(field.locator?.name || "")) continue;

    const normalizedQuery = field.query.toLowerCase().trim();
    const existing = userContextVault.find((e) => {
      if (e.key.toLowerCase() === normalizedQuery) return true;
      if (e.aliases && e.aliases.some((a) => normalizedQuery.includes(a.toLowerCase()))) return true;
      if (field.constraints?.autocomplete && e.autocomplete === field.constraints.autocomplete) return true;
      return false;
    });

    const pageTitle = payload.title || "active page";
    const pageUrl = payload.url || (tab && tab.url) || "";

    if (!existing) {
      newDeltas.push({
        deltaId: `delta_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
        action: "create",
        category: "custom",
        key: field.query,
        proposedValue: String(field.value),
        evidenceSnippet: `Submitted on form "${pageTitle}"`,
        sourceUrl: pageUrl,
        confidence: 0.88
      });
    } else if (existing.value !== String(field.value)) {
      newDeltas.push({
        deltaId: `delta_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`,
        action: "update",
        targetEntryId: existing.id,
        category: existing.category,
        key: existing.key,
        previousValue: existing.value,
        proposedValue: String(field.value),
        evidenceSnippet: `Observed updated value on form "${pageTitle}"`,
        sourceUrl: pageUrl,
        confidence: 0.94
      });
    }
  }

  if (newDeltas.length > 0) {
    const merged = [...newDeltas, ...pendingDeltas].slice(0, 100);
    await chrome.storage.local.set({ pendingDeltas: merged });
    updateBadge(merged.length);
  }

  return { queuedDeltas: newDeltas.length };
}
