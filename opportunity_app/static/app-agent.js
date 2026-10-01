// Agent: threads, the conversation and the activity audit.
(() => {
  "use strict";

  const App = window.OpportunityApp;

  // From app-context.js.
  const { els, registerViewHandlers, state } = App;

  // From app-ui.js.
  const { chip, element, formatDate, optionElement, showError } = App;

  // From app-http.js.
  const { api } = App;

  // From app-status.js.
  const { loadStats } = App;

  // From app-nav.js.
  const { loadingLine, runViewLoad, sectionSubnav, tagSection } = App;

  async function loadAgent() {
    await runViewLoad({ views: ["agent"], placeholder: loadingLine("Loading agent history…") }, async ({ isCurrent }) => {
      const [threadsPayload, activityPayload, providersPayload] = await Promise.all([
        api("/api/v1/agent/threads"),
        api("/api/v1/agent/activity"),
        api("/api/v1/agent/providers"),
      ]);
      if (!isCurrent()) return;
      const threads = threadsPayload.items || [];
      const providers = (providersPayload.items || []).filter((provider) => provider.configured);
      if (!state.agentThreadId || !threads.some((thread) => thread.id === state.agentThreadId)) {
        state.agentThreadId = threads[0]?.id || null;
      }
      els.results.replaceChildren();
      els.resultCount.textContent = "Student agent";
      els.pageStatus.textContent = "No mutation runs without your approval";

      const shell = element("section", "agent-shell");
      const sidebar = element("aside", "agent-thread-list");
      const providerSelect = document.createElement("select");
      providerSelect.className = "agent-provider-select";
      providerSelect.setAttribute("aria-label", "Agent provider for new thread");
      providerSelect.appendChild(optionElement("legacy", "Built-in grounded assistant · no API key"));
      providers.forEach((provider) => {
        providerSelect.appendChild(optionElement(provider.id, `${provider.display_name} · ${provider.model}`));
      });
      const newThread = element("button", "secondary-button", "New thread");
      newThread.type = "button";
      newThread.addEventListener("click", async () => {
        newThread.disabled = true;
        try {
          const created = await api("/api/v1/agent/threads", {
            method: "POST",
            body: JSON.stringify({ title: "Career planning", provider: providerSelect.value }),
          });
          state.agentThreadId = created.id;
          await loadAgent();
        } catch (error) {
          showError(error.message);
          newThread.disabled = false;
        }
      });
      sidebar.append(providerSelect, newThread);
      if (!providers.length) {
        sidebar.appendChild(element("p", "empty-inline", "The built-in grounded assistant is ready. For a model-backed thread, sign in to Claude Code (`claude`) or Codex CLI (`codex login`) on your subscription, or add OPENAI_API_KEY or ANTHROPIC_API_KEY to .env."));
      }
      threads.forEach((thread) => {
        const button = element("button", `agent-thread-button ${thread.id === state.agentThreadId ? "is-active" : ""}`.trim());
        button.type = "button";
        button.appendChild(element("strong", "", thread.title));
        button.appendChild(element("span", "", `${thread.provider} · ${thread.status} · ${thread.messages_used}/${thread.message_budget} messages`));
        button.addEventListener("click", () => {
          state.agentThreadId = thread.id;
          loadAgent();
        });
        sidebar.appendChild(button);
      });

      const main = element("div", "agent-main");
      const thread = threads.find((item) => item.id === state.agentThreadId);
      if (!thread) {
        const empty = element("div", "empty-state");
        empty.appendChild(element("strong", "", "Start an agent thread"));
        empty.appendChild(element("p", "", "Threads preserve messages, tool runs, budgets, and every proposed action."));
        main.appendChild(empty);
      } else {
        const toolbar = element("div", "agent-toolbar");
        const title = element("div", "");
        title.appendChild(element("h3", "", thread.title));
        title.appendChild(element("p", "", `${thread.tools_used}/${thread.tool_budget} tool calls used`));
        toolbar.appendChild(title);
        if (thread.status === "active") {
          const cancel = element("button", "danger-button", "Cancel thread");
          cancel.type = "button";
          cancel.addEventListener("click", async () => {
            if (!window.confirm("Cancel this thread and reject its pending actions?")) return;
            await api(`/api/v1/agent/threads/${encodeURIComponent(thread.id)}/cancel`, { method: "POST" });
            await loadAgent();
          });
          toolbar.appendChild(cancel);
        }
        main.appendChild(toolbar);

        const messages = element("div", "agent-messages");
        thread.messages.forEach((message) => {
          const bubble = element("article", `agent-message is-${message.role}`);
          bubble.appendChild(element("p", "agent-role", message.role === "user" ? "You" : "Pipeline agent"));
          bubble.appendChild(element("p", "agent-content", message.content));
          if (message.citations?.length) {
            const citations = element("div", "agent-citations");
            message.citations.forEach((citation) => {
              citations.appendChild(chip(
                citation.id ? `${citation.type}:${citation.id}` : citation.field ? `profile.${citation.field}` : citation.type,
                "is-region"
              ));
            });
            bubble.appendChild(citations);
          }
          messages.appendChild(bubble);
        });
        if (!thread.messages.length) messages.appendChild(element("p", "empty-inline", "Ask about top matches, profile gaps, deadlines, or next tasks."));
        main.appendChild(messages);

        const pendingActions = thread.proposed_actions.filter((action) => action.status === "pending");
        if (pendingActions.length) {
          const proposals = element("section", "agent-proposals");
          proposals.appendChild(element("p", "eyebrow", "Awaiting your decision"));
          pendingActions.forEach((proposal) => {
            const card = element("article", "proposal-card");
            card.appendChild(element("strong", "", proposal.action_type.replaceAll("_", " ")));
            card.appendChild(element("p", "", proposal.expected_effect));
            card.appendChild(element("code", "", proposal.scope));
            const controls = element("div", "preparation-actions");
            const approve = element("button", "secondary-button", "Approve");
            approve.type = "button";
            const reject = element("button", "danger-button", "Reject");
            reject.type = "button";
            const decide = async (decision) => {
              approve.disabled = true;
              reject.disabled = true;
              try {
                await api(`/api/v1/agent/proposals/${encodeURIComponent(proposal.id)}/decision`, {
                  method: "POST",
                  body: JSON.stringify({ decision }),
                });
                await Promise.all([loadAgent(), loadStats()]);
              } catch (error) {
                showError(error.message);
                approve.disabled = false;
                reject.disabled = false;
              }
            };
            approve.addEventListener("click", () => decide("approve"));
            reject.addEventListener("click", () => decide("reject"));
            controls.append(approve, reject);
            card.appendChild(controls);
            proposals.appendChild(card);
          });
          main.appendChild(proposals);
        }

        if (thread.status === "active") {
          const prompts = element("div", "agent-quick-prompts");
          ["What should I apply to?", "What am I missing?", "What closes soon?", "What should I do next?"].forEach((prompt) => {
            const button = element("button", "chip", prompt);
            button.type = "button";
            prompts.appendChild(button);
          });
          const composer = element("form", "agent-composer");
          const input = document.createElement("textarea");
          input.placeholder = "Ask about your evidence-backed pipeline";
          input.required = true;
          const send = element("button", "primary-button", "Send");
          send.type = "submit";
          const composerStatus = element("p", "form-status");
          const sendContent = async (content) => {
            send.disabled = true;
            composerStatus.textContent = "Checking your workspace…";
            try {
              await api(`/api/v1/agent/threads/${encodeURIComponent(thread.id)}/messages`, {
                method: "POST",
                body: JSON.stringify({ content }),
              });
              await loadAgent();
            } catch (error) {
              composerStatus.textContent = error.message;
              send.disabled = false;
              await loadAgent();
            }
          };
          prompts.querySelectorAll("button").forEach((button) => button.addEventListener("click", () => sendContent(button.textContent)));
          composer.addEventListener("submit", (event) => {
            event.preventDefault();
            sendContent(input.value);
          });
          composer.append(input, send, composerStatus);
          main.append(prompts, composer);
        }
      }
      shell.append(sidebar, main);

      const activity = element("section", "profile-card agent-activity");
      activity.appendChild(element("p", "eyebrow", "Background activity"));
      activity.appendChild(element("h3", "", "Tool and approval audit"));
      const activityList = element("ol", "timeline-list");
      activityPayload.items.forEach((item) => {
        const row = element("li", "");
        row.appendChild(element("strong", "", item.label.replaceAll("_", " ")));
        row.appendChild(element("span", "", `${item.status} · ${formatDate(item.created_at)}`));
        activityList.appendChild(row);
      });
      if (!activityPayload.items.length) activityList.appendChild(element("li", "", "No tool activity yet."));
      activity.appendChild(activityList);
      els.results.append(tagSection(shell, "chat", "Conversation"), tagSection(activity, "activity", "Tool and approval audit"));
      sectionSubnav();
      els.results.setAttribute("aria-busy", "false");
    });
  }

  // What this file does as it loads, run once by app.js in load order.
  function installAgent() {
    registerViewHandlers("agent", { load: loadAgent });
  }

  Object.assign(App, {
    installAgent,
  });
})();
