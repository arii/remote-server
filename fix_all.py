import re
with open("buttons/server.py", "r") as f:
    c = f.read()

# Remove old chats
chats = ['briefingChatCard', 'piChatCard']
for ch in chats:
    pattern = r'<div class="chat-card" id="' + ch + r'">.*?</div>\n</div>'
    c = re.sub(pattern, '', c, flags=re.DOTALL)

# Remove actions button block
button_block_pattern = r'<!--BUTTONS-->.*?<!--ENDBUTTONS-->'
c = re.sub(button_block_pattern, '<!--BUTTONS--><!--ENDBUTTONS-->', c, flags=re.DOTALL)


# Add proactive flag to prompt
prompt_pattern = r'messages\.append\(\{"role": "user", "content": prompt\}\)'
new_prompt = '''if "__PROACTIVE_MODE__" in prompt:
            prompt = "I have just opened the dashboard. What needs my attention right now? Please suggest actions."
            sys_msg = "\\n\\nYou must evaluate the current briefing and repo state. If there are things that need attention (failing CI, conflicts, open PRs, etc.), respond with a JSON block containing an 'actions' array. Available actions: " + ", ".join(ACTIONS.keys()) + "\\n\\nFormat: ```json\\n{\\n  \\"text\\": \\"Here is what needs attention...\\",\\n  \\"actions\\": [\\"sync-agents\\", \\"local-repair\\"]\\n}\\n```"
            messages[0]["content"] += sys_msg
        messages.append({"role": "user", "content": prompt})'''
c = c.replace('messages.append({"role": "user", "content": prompt})', new_prompt)

# Add JSON rendering logic
render_markdown_patch = """  function renderFullMarkdown(container, text) {
    container.innerHTML = "";
    activeTextNode = null;
    if (!text) return;

    // Check if the text contains a JSON block with actions
    const jsonMatch = text.match(/```(?:json)?\\n([\\s\\S]*?)\\n```/);
    if (jsonMatch) {
      try {
        const parsed = JSON.parse(jsonMatch[1]);
        if (parsed.actions && Array.isArray(parsed.actions)) {
          // Render text part
          if (parsed.text) {
             const t = document.createElement("div");
             t.textContent = parsed.text;
             container.appendChild(t);
          }

          // Render inline buttons
          const actionsWrap = document.createElement("div");
          actionsWrap.style.marginTop = "1em";
          actionsWrap.style.display = "flex";
          actionsWrap.style.gap = "0.5em";
          actionsWrap.style.flexWrap = "wrap";

          parsed.actions.forEach(actionName => {
             const btn = document.createElement("button");
             btn.type = "button";
             btn.className = "btn-primary";
             btn.textContent = "⚡ " + actionName;
             btn.onclick = () => {
                 fetchJson("/buttons/api/run", {
                     method: "POST",
                     body: JSON.stringify({ action: actionName, repo: STATE.selectedRepo })
                 }).then(res => {
                     if (res.id) {
                         alert("Action triggered: " + res.id);
                     } else {
                         alert("Error: " + (res.error || "Unknown error"));
                     }
                 });
             };
             actionsWrap.appendChild(btn);
          });
          container.appendChild(actionsWrap);
          return;
        }
      } catch (e) {
          // Fall back to normal rendering if JSON is invalid
      }
    }"""
c = c.replace('  function renderFullMarkdown(container, text) {', render_markdown_patch)

with open("buttons/server.py", "w") as f:
    f.write(c)
