from skills.gmail import gmail
print(gmail.reply('reply to the latest email saying hello', draft_mode=True, confirm=False))
