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

const armConfirmation = (panel) => {
  panel.$("submitted-confirm").checked = true;
  panel.$("submitted-confirm").dispatch("change");
};

const assertPageChangeDroppedOnlyTheScan = (panel) => {
  assert.equal(panel.$("fields").children.length, 0, "app-a's proposed fields are gone");
  assert.equal(panel.$("review-form").hidden, true);
  assert.equal(panel.$("progress").hidden, true);
  assert.equal(panel.$("submitted-confirm").checked, false, "the confirmation checkbox is unticked");
  assert.equal(panel.$("mark-submitted").disabled, true, "Mark as submitted waits until the box is ticked again");
  assert.equal(panel.$("context").hidden, false, "the chosen application's card stays");
  assert.match(panel.$("match-card").textContent, /Acme Robotics/, "the card still names app-a");
  assert.match(
    panel.$("status").textContent,
    /The page changed\. Mark as submitted confirms Acme Robotics — Software Intern; tick the box again if you submitted it\./,
    "the status names the application Mark as submitted would confirm",
  );
};

sidepanelTests.a_page_change_drops_the_scan_and_the_tick_but_keeps_the_application_it_names = async () => {
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  armConfirmation(panel);
  assert.equal(panel.$("mark-submitted").disabled, false, "set-up: the button was armed for app-a");

  await panel.navigate("https://jobs.example.com/apply/2");

  assertPageChangeDroppedOnlyTheScan(panel);
  assert.equal(panel.$("candidates").children.length, 0, "the old page's candidates are gone");
};

sidepanelTests.a_navigation_without_a_url_still_drops_the_scan_and_the_tick = async () => {
  // Without host permission for the page Chrome reports only status: "loading".
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  armConfirmation(panel);

  await panel.navigate("https://jobs.example.com/apply/2", { statusOnly: true });

  assertPageChangeDroppedOnlyTheScan(panel);
};

sidepanelTests.switching_tabs_drops_the_scan_and_the_tick_but_keeps_the_application_it_names = async () => {
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  armConfirmation(panel);

  await panel.activateTab(8, "https://other.example.com/apply/9");

  assertPageChangeDroppedOnlyTheScan(panel);
};

sidepanelTests.scanning_in_another_tab_than_the_one_the_application_was_chosen_in_clears_the_choice_and_refuses = async () => {
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  const before = panel.requests.length;

  await panel.activateTab(8, "https://other.example.com/apply/9");
  await panel.scan();

  assert.deepEqual(
    panel.requests.slice(before).filter((item) => item.method === "PUT"),
    [],
    "no session was written for app-a with another tab's page_url",
  );
  assert.equal(panel.$("context").hidden, true, "the choice is cleared");
  assert.equal(panel.$("review-form").hidden, true);
  assert.match(panel.$("status").textContent, /find/i, "the student is asked to find the application again");
  await panel.forceMarkSubmitted();
  assert.deepEqual(panel.confirmRequests(), [], "nothing can be confirmed from the cleared choice");
};

sidepanelTests.a_candidates_answer_that_arrives_after_the_page_changed_is_dropped = async () => {
  const panel = await loadSidepanel({ applications });
  panel.holdNextCandidates();
  panel.startFinding();
  await settle();
  await panel.navigate("https://jobs.example.com/apply/2");
  panel.releaseCandidates();
  await settle();

  assert.equal(panel.$("candidates").children.length, 0, "the old page's candidates were not listed for the new page");
  assert.doesNotMatch(panel.$("status").textContent, /Choose the application|exact match/i);
};

sidepanelTests.submitting_then_landing_on_a_thanks_page_still_confirms_the_scanned_application = async () => {
  // The normal path: most applications navigate to a confirmation page right after Submit.
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  const sessionA = sessionPath(panel, "app-a");
  armConfirmation(panel);

  await panel.navigate("https://jobs.example.com/apply/1/thanks");
  assert.equal(panel.$("mark-submitted").disabled, true, "the page change unticked the box");
  await panel.tickAndMarkSubmitted();

  const confirms = panel.confirmRequests();
  assert.equal(confirms.length, 1);
  assert.equal(confirms[0].path, `${sessionA}/confirm-submitted`);
};

sidepanelTests.a_url_change_within_the_same_tab_keeps_a_multi_step_form_working = async () => {
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  await panel.navigate("https://jobs.example.com/apply/1/step-2");
  await panel.scan();

  const synced = panel.requests.filter((item) => item.method === "PUT" && item.body?.application_id);
  assert.equal(synced.at(-1).body.application_id, "app-a");
  assert.equal(synced.at(-1).body.page_url, "https://jobs.example.com/apply/1/step-2");
  assert.equal(panel.$("review-form").hidden, false, "step 2 was scanned for the same application");
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

sidepanelTests.a_page_change_while_a_fill_syncs_still_records_the_fill_and_keeps_the_status = async () => {
  // Submitting usually navigates away within milliseconds, so the page can change while the fill is still being saved.
  const panel = await loadSidepanel({ applications });
  await panel.findApplications();
  await panel.chooseApplication("app-a");
  await panel.scan();
  panel.holdNextDigest();
  panel.$("review-form").dispatch("submit", { preventDefault() {} });
  await settle();
  await panel.navigate("https://jobs.example.com/apply/1/thanks");
  panel.releaseDigest();
  await settle();
  await settle();

  const filled = panel.requests.filter((item) => item.method === "PUT" && item.path.includes("/steps/") && item.body?.status === "filled");
  assert.equal(filled.length, 1, "the fill that finished after the page changed is still recorded");
  assert.equal(filled[0].body.page_url, "https://jobs.example.com/apply/1", "against the page it was filled on");
  assert.match(panel.$("status").textContent, /The page changed/, "the status still names what Mark as submitted confirms");
};
