import re
import os

with open("buttons/server.py", "r") as f:
    content = f.read()

# 1. Move actionsChatCard to stickyHead
# Extract actionsChatCard
actions_card_match = re.search(r'<div class="chat-card" id="actionsChatCard">.*?</textarea>.*?</div>\s*</div>\s*<div id="actionsChatStream" class="chat-stream-box"></div>\s*<div id="actionsChatErr" class="repair-err" style="margin-top:0.4em;"></div>\s*</div>', content, re.DOTALL)
actions_card_html = actions_card_match.group(0)

# Remove actionsChatCard from tab-actions
content = content.replace(actions_card_html, "")

# Insert actionsChatCard into stickyHead (after pickerBar, tabbar)
sticky_head_end = content.find('</div>\n<section class="tabpanel" id="tab-actions">')
content = content[:sticky_head_end] + actions_card_html + "\n" + content[sticky_head_end:]


# 2. Remove briefingChatCard and piChatCard
briefing_card_match = re.search(r'<div class="chat-card" id="briefingChatCard">.*?</div>\s*</div>', content, re.DOTALL)
if briefing_card_match:
    content = content.replace(briefing_card_match.group(0), "")

pi_card_match = re.search(r'<div class="chat-card" id="piChatCard">.*?</div>\s*</div>', content, re.DOTALL)
if pi_card_match:
    content = content.replace(pi_card_match.group(0), "")

# 3. Modify JS to handle JSON actions
# Find `renderFullMarkdown` in JS and modify
markdown_render_js_old = """function renderFullMarkdown(text) {
  // basic regex for code blocks, bold, etc."""
# Wait, let's just find the renderFullMarkdown function
import json
pass
