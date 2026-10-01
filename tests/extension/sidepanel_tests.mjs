// Behavioral tests for apps/extension/sidepanel.js (see sidepanel_harness.mjs).
import assert from "node:assert/strict";
import { loadSidepanel, settle } from "./sidepanel_harness.mjs";

const applications = [
  { id: "app-a", company: "Acme Robotics", title: "Software Intern" },
  { id: "app-b", company: "Orbit Systems", title: "Research Intern" },
];

export const sidepanelTests = {};

const sessionPath = (panel, applicationId) => {
  const put = panel.requests.find((item) => item.method === "PUT" && item.body?.application_id === applicationId);
  assert.ok(put, `a session was synced for ${applicationId}`);
  return put.path;
};

sidepanelTests.sidepanel_confirm_names_the_session_the_scan_created = async () => {
  // The instrument check: the harness drives scan, then confirm, through the real handlers.
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  assert.equal(panel.$("review-form").hidden, false, "the scan rendered the review form");
  await panel.tickAndMarkSubmitted();
  const confirms = panel.confirmRequests();
  assert.equal(confirms.length, 1);
  assert.equal(confirms[0].path, `${sessionPath(panel, "app-a")}/confirm-submitted`);
};

sidepanelTests.choosing_another_application_after_a_scan_cannot_mark_the_old_one_submitted = async () => {
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  const sessionA = sessionPath(panel, "app-a");
  await panel.chooseApplication("app-b");

  // Even if the handler is reached with the box ticked, no request may name app-a's session.
  await panel.forceMarkSubmitted();
  assert.deepEqual(panel.confirmRequests().map((item) => item.path), [], `confirm-submitted must not be sent (it named ${sessionA})`);
  assert.match(panel.$("status").textContent, /scan/i, "the panel tells the student to scan first");
};

sidepanelTests.choosing_another_application_clears_everything_tied_to_the_previous_scan = async () => {
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  panel.$("submitted-confirm").checked = true;
  panel.$("submitted-confirm").dispatch("change");
  assert.equal(panel.$("mark-submitted").disabled, false, "set-up: the button was armed for app-a");

  await panel.chooseApplication("app-b");

  assert.equal(panel.$("fields").children.length, 0, "app-a's proposed fields are gone");
  assert.equal(panel.$("review-form").hidden, true);
  assert.equal(panel.$("progress").hidden, true, "app-a's progress summary is gone");
  assert.equal(panel.$("download-fallback").hidden, true, "app-a's document download link is gone");
  assert.equal(panel.$("submitted-confirm").checked, false, "the confirmation checkbox is reset");
  assert.equal(panel.$("mark-submitted").disabled, true, "Mark as submitted needs a new scan and a new confirmation");
};

sidepanelTests.scanning_the_newly_chosen_application_confirms_that_application = async () => {
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  await panel.chooseApplication("app-b");
  await panel.scan();
  await panel.tickAndMarkSubmitted();
  const confirms = panel.confirmRequests();
  assert.equal(confirms.length, 1);
  assert.notEqual(sessionPath(panel, "app-a"), sessionPath(panel, "app-b"));
  assert.equal(confirms[0].path, `${sessionPath(panel, "app-b")}/confirm-submitted`);
};

sidepanelTests.a_slow_context_for_the_application_left_behind_cannot_overwrite_the_current_one = async () => {
  const panel = await loadSidepanel({ applications, slowContext: ["app-a"] });
  await panel.findApplications();
  panel.startChoosing("app-a");
  await panel.chooseApplication("app-b");
  panel.release("app-a");
  await panel.scan();
  assert.match(panel.$("match-card").textContent, /Orbit Systems/, "the card still shows app-b after app-a's late answer");
  const synced = panel.requests.filter((item) => item.method === "PUT" && item.body?.application_id);
  assert.ok(synced.length > 0 && synced.every((item) => item.body.application_id === "app-b"), "the scan syncs against app-b only");
};

sidepanelTests.choosing_another_application_during_a_scan_cannot_mark_the_old_one_submitted = async () => {
  // The scan of app-a is still working out its session id when the student chooses app-b.
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  const sessionA = sessionPath(panel, "app-a");
  panel.holdNextDigest();
  panel.$("scan").click();
  await settle();
  await panel.chooseApplication("app-b");
  panel.releaseDigest();
  await settle();

  assert.match(panel.$("match-card").textContent, /Orbit Systems/, "the card shows app-b");
  assert.equal(panel.$("review-form").hidden, true, "app-a's late scan did not render its fields");
  await panel.forceMarkSubmitted();
  assert.deepEqual(panel.confirmRequests().map((item) => item.path), [], `confirm-submitted must not be sent (it named ${sessionA})`);
  assert.match(panel.$("status").textContent, /scan/i, "the panel tells the student to scan first");
};

sidepanelTests.a_late_attach_for_the_application_left_behind_leaves_no_download_link = async () => {
  const panel = await loadSidepanel({ applications, withDocument: true });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  assert.equal(panel.$("documents").hidden, false, "set-up: app-a offers its document");
  panel.holdNextAttach();
  panel.startAttach();
  await settle();
  await panel.chooseApplication("app-b");
  await panel.scan();
  panel.releaseAttach();
  await settle();

  assert.equal(panel.$("download-fallback").hidden, true, "app-a's late answer did not bring back its download link");
  assert.doesNotMatch(panel.$("status").textContent, /resume-app-a|manual download/i, "the status is not app-a's attach error");
};

sidepanelTests.moving_to_another_page_after_a_scan_cannot_mark_the_old_application_submitted = async () => {
  // Scan A, open another application's page, find candidates, pick nothing, tick the box: the
  // session on screen is still A's scan of A's page, and must not be what gets confirmed.
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  const sessionA = sessionPath(panel, "app-a");

  await panel.navigate("https://jobs.example.com/apply/2");
  await panel.findApplications();
  await panel.forceMarkSubmitted();

  assert.deepEqual(panel.confirmRequests().map((item) => item.path), [], `confirm-submitted must not be sent (it named ${sessionA})`);
  assert.match(panel.$("status").textContent, /scan/i, "the panel tells the student to scan first");
};

sidepanelTests.moving_to_another_page_clears_the_chosen_application_and_everything_tied_to_its_scan = async () => {
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  panel.$("submitted-confirm").checked = true;
  panel.$("submitted-confirm").dispatch("change");
  assert.equal(panel.$("mark-submitted").disabled, false, "set-up: the button was armed for app-a");

  await panel.navigate("https://jobs.example.com/apply/2");

  assert.equal(panel.$("fields").children.length, 0, "app-a's proposed fields are gone");
  assert.equal(panel.$("review-form").hidden, true);
  assert.equal(panel.$("context").hidden, true, "the chosen application's card is gone: the student chooses again");
  assert.equal(panel.$("candidates").children.length, 0, "the old page's candidates are gone");
  assert.equal(panel.$("submitted-confirm").checked, false, "the confirmation checkbox is reset");
  assert.equal(panel.$("mark-submitted").disabled, true, "Mark as submitted needs a new scan and a new confirmation");
  assert.match(panel.$("status").textContent, /choose/i, "the panel tells the student to choose the application again");

  // Scanning with nothing chosen is refused rather than reusing app-a.
  const before = panel.requests.length;
  await panel.scan();
  assert.equal(panel.requests.length, before, "no scan session was synced without a chosen application");
};

sidepanelTests.after_moving_to_another_page_choosing_and_scanning_confirms_the_new_session = async () => {
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  await panel.navigate("https://jobs.example.com/apply/2");
  await panel.findApplications();
  await panel.chooseApplication("app-b");
  await panel.scan();
  await panel.tickAndMarkSubmitted();
  const confirms = panel.confirmRequests();
  assert.equal(confirms.length, 1);
  assert.equal(confirms[0].path, `${sessionPath(panel, "app-b")}/confirm-submitted`);
};

sidepanelTests.finding_candidates_again_drops_the_scan_of_the_application_chosen_before = async () => {
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  const sessionA = sessionPath(panel, "app-a");

  await panel.findApplications();
  await panel.forceMarkSubmitted();

  assert.deepEqual(panel.confirmRequests().map((item) => item.path), [], `confirm-submitted must not be sent (it named ${sessionA})`);
  assert.equal(panel.$("review-form").hidden, true, "app-a's scan is not left on screen under a new list of candidates");
};
