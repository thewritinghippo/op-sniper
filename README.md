# OP Sniper (GitHub version)

Books One Playground classes the moment their 72-hour booking window opens, running on GitHub so your laptop can be off. You manage it from a web page that works on your phone.

How it works: GitHub runs a check every 15 minutes. When a booking window is less than 75 minutes away, that run stays awake, signs in just before the window and books on the second. Your OP login is a GitHub secret. Your classes and results are kept in private repo variables, and the page only shows them once you unlock it with your GitHub key. The run logs on GitHub only say things like "BOOKED", never which class or gym.

Setup steps are in the chat. Things to know:

- GitHub can start scheduled runs late, which is why each run looks 75 minutes ahead. If the page says no check has happened in over 90 minutes, open the Actions tab and make sure the workflow is switched on.
- The GitHub key you make expires. When the page says the key was turned down, make a new one, update the GH_TOKEN secret, and paste the new key into the page.
- It never books anything less than 30 hours away, to stay clear of the late-cancel fee.
- Don't run the laptop version at the same time.
- `opsniper.py` still works on its own on a computer too (`py -3.12 opsniper.py web`).
