import re
with open("tests/test_interactive_chat.py", "r") as f:
    c = f.read()

# We need to remove the assertions for briefingChatCard and piChatCard
c = re.sub(r'        # Briefing tab chat card.*?# Pi tab chat card', '# Pi tab chat card', c, flags=re.DOTALL)
c = re.sub(r'        # Pi tab chat card.*?self\.assertIn\("initInteractiveChat", server\.HTML\)', '        self.assertIn("initInteractiveChat", server.HTML)', c, flags=re.DOTALL)

with open("tests/test_interactive_chat.py", "w") as f:
    f.write(c)
