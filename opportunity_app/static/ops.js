(() => {
  "use strict";
  const role = location.pathname === "/admin" ? "admin" : "employer";
  const token = document.getElementById("role-token");
  const status = document.getElementById("access-status");
  const employerTools = document.getElementById("employer-tools");
  const adminTools = document.getElementById("admin-tools");
  document.getElementById("workspace-title").textContent = role === "admin" ? "Admin operations" : "Employer workspace";

  async function request(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {"Authorization": `Bearer ${token.value}`, "Content-Type": "application/json", ...(options.headers || {})},
    });
    const body = response.status === 204 ? null : await response.json();
    if (!response.ok) throw new Error(body?.detail || `Request failed (${response.status})`);
    return body;
  }

  async function openWorkspace() {
    const endpoint = role === "admin" ? "/api/v1/admin/overview" : "/api/v1/employer/requisitions";
    const body = await request(endpoint);
    status.textContent = `${role} access confirmed.`;
    employerTools.hidden = role !== "employer";
    adminTools.hidden = role !== "admin";
    document.getElementById(role === "admin" ? "admin-output" : "employer-output").textContent = JSON.stringify(body, null, 2);
  }

  document.getElementById("access-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await openWorkspace(); } catch (error) { status.textContent = error.message; }
  });
  document.getElementById("organization-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const body = await request("/api/v1/employer/organizations", {method: "POST", body: JSON.stringify({name: document.getElementById("organization-name").value, organization_type: "employer"})});
      document.getElementById("organization-id").value = body.id;
      document.getElementById("employer-output").textContent = JSON.stringify(body, null, 2);
    } catch (error) { status.textContent = error.message; }
  });
  document.getElementById("requisition-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const organizationId = document.getElementById("organization-id").value;
      const body = await request(`/api/v1/employer/organizations/${encodeURIComponent(organizationId)}/requisitions`, {method: "POST", body: JSON.stringify({title: document.getElementById("requisition-title").value, rubric: [{criterion: document.getElementById("rubric-criterion").value, weight: 100}]})});
      document.getElementById("employer-output").textContent = JSON.stringify(body, null, 2);
    } catch (error) { status.textContent = error.message; }
  });
  document.getElementById("candidate-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const req = document.getElementById("candidate-requisition").value;
      const body = await request(`/api/v1/employer/requisitions/${encodeURIComponent(req)}/candidates`, {method: "POST", body: JSON.stringify({share_token: document.getElementById("candidate-share").value})});
      document.getElementById("decision-candidate").value = body.id;
      document.getElementById("employer-output").textContent = JSON.stringify(body, null, 2);
    } catch (error) { status.textContent = error.message; }
  });
  document.getElementById("decision-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const id = document.getElementById("decision-candidate").value;
      const body = await request(`/api/v1/employer/candidates/${encodeURIComponent(id)}/decision`, {method: "POST", body: JSON.stringify({status: document.getElementById("decision-status").value, reason: document.getElementById("decision-reason").value})});
      document.getElementById("employer-output").textContent = JSON.stringify(body, null, 2);
    } catch (error) { status.textContent = error.message; }
  });
  document.getElementById("refresh-admin").addEventListener("click", async () => {
    try { const [overview, school] = await Promise.all([request("/api/v1/admin/overview"), request("/api/v1/admin/school-report")]); document.getElementById("admin-output").textContent = JSON.stringify({overview, school}, null, 2); }
    catch (error) { status.textContent = error.message; }
  });
  document.getElementById("verify-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { document.getElementById("admin-output").textContent = JSON.stringify(await request(`/api/v1/admin/organizations/${encodeURIComponent(document.getElementById("verify-organization").value)}/verify`, {method: "POST", body: JSON.stringify({approved: true})}), null, 2); }
    catch (error) { status.textContent = error.message; }
  });
  document.getElementById("source-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { document.getElementById("admin-output").textContent = JSON.stringify(await request(`/api/v1/admin/sources/${encodeURIComponent(document.getElementById("source-key").value)}`, {method: "PUT", body: JSON.stringify({enabled: document.getElementById("source-enabled").checked, moderation_status: document.getElementById("source-status").value, note: document.getElementById("source-note").value})}), null, 2); }
    catch (error) { status.textContent = error.message; }
  });
  document.getElementById("moderation-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { document.getElementById("admin-output").textContent = JSON.stringify(await request("/api/v1/admin/moderation", {method: "POST", body: JSON.stringify({target_type: document.getElementById("moderation-type").value, target_id: document.getElementById("moderation-target").value, reason: document.getElementById("moderation-reason").value})}), null, 2); }
    catch (error) { status.textContent = error.message; }
  });
})();
