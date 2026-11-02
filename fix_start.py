import re
with open("buttons/server.py", "r") as f:
    c = f.read()

# Make chat start proactive
start_chat_patch = """  function startChat(proactive = false) {
    CHAT_ACTIVE = true;
    CHAT_SESSION_ID = crypto.randomUUID();
    const chatInput = document.getElementById("actionsChatInput");
    const abortBtn = document.getElementById("actionsChatAbortBtn");
    const sendBtn = document.getElementById("actionsChatSendBtn");
    const streamBox = document.getElementById("actionsChatStream");
    const errBox = document.getElementById("actionsChatErr");

    streamBox.innerHTML = "";
    errBox.textContent = "";

    let prompt = "";
    if (proactive) {
        prompt = "__PROACTIVE_MODE__";
    } else {
        prompt = chatInput.value.trim();
        if (!prompt) return;
        chatInput.value = "";
    }
"""

c = c.replace('  function startChat() {\n    const chatInput = document.getElementById("actionsChatInput");', start_chat_patch)

with open("buttons/server.py", "w") as f:
    f.write(c)
