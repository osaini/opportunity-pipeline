(() => {
  "use strict";
  const families = Object.freeze([
    { id: "workday", hosts: ["myworkdayjobs.com", "workday.com"] },
    { id: "greenhouse", hosts: ["greenhouse.io"] },
    { id: "lever", hosts: ["lever.co"] },
    { id: "ashby", hosts: ["ashbyhq.com"] },
    { id: "smartrecruiters", hosts: ["smartrecruiters.com"] },
    { id: "icims", hosts: ["icims.com"], experimental: true },
    { id: "workable", hosts: ["workable.com"], experimental: true },
  ]);
  const mappings = Object.freeze([
    { pattern: /\b(first.?name|given.?name)\b/i, field: "name.first" },
    { pattern: /\b(last.?name|family.?name|surname)\b/i, field: "name.last" },
    { pattern: /^(?:name\s*){1,3}$|\b(full.?name|legal.?name|applicant.?name|candidate.?name)\b/i, field: "name.full" },
    { pattern: /\bemail\b/i, field: "contact.email" },
    { pattern: /\b(phone|mobile|telephone)\b/i, field: "contact.phone" },
    { pattern: /\blinkedin\b/i, field: "contact.linkedin" },
    { pattern: /\bgithub\b/i, field: "contact.github" },
    { pattern: /\b(portfolio|personal website|website)\b/i, field: "contact.portfolio" },
    { pattern: /\b(street|address line.?1|mailing address)\b/i, field: "contact.address.street" },
    { pattern: /\b(city|town)\b/i, field: "contact.address.city" },
    { pattern: /\b(state|province)\b/i, field: "contact.address.state" },
    { pattern: /\b(zip|postal)\b/i, field: "contact.address.postal_code" },
    { pattern: /\bcountry\b/i, field: "contact.address.country" },
    { pattern: /\b(school|university|college)\b/i, field: "school" },
    { pattern: /\b(degree|program|major|field of study)\b/i, field: "degree" },
    { pattern: /\b(graduation|graduate.?year)\b/i, field: "graduation_year" },
  ]);
  function detect(hostname) {
    const host = String(hostname || "").toLowerCase();
    return families.find((family) => family.hosts.some((suffix) => host === suffix || host.endsWith(`.${suffix}`))) || { id: "generic", hosts: [] };
  }
  globalThis.OpportunityApplyAdapters = Object.freeze({ families, mappings, detect });
})();
