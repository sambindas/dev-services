# Dev Services local agent (preview)

Requires Python 3.10+ and your project's normal runtimes. Native packaged installers
from older releases do not include this agent yet.

1. Extract this folder, then run `python3 -m pip install -r requirements.txt`.
2. Run `python3 server.py`. Add your projects through its local browser dashboard.
3. In another terminal, from this same folder, run
   `python3 agent.py --pair https://YOUR-HOSTED-DASHBOARD`.
4. Type PAIR, then paste the one-time code from **Pair computer** on your hosted account.
5. Keep the agent running. On future starts, run `python3 agent.py`.

The local dashboard must also stay running. To pair an existing source dashboard,
update it to this version first and add `--local-url http://127.0.0.1:7777` (or its
actual local port). The agent uses that same controller, so local and remote
actions share process ownership and busy state. It does not manage a second set
of project processes. `AGENT_DATA_DIR` can choose where its pairing file is kept.

On Windows use `py` instead of `python3`. Use a virtual environment if your Python
installation requires one. You can also copy these source files into an existing
Dev Services source installation to use its configuration; preserve services.json.

The agent makes outbound HTTPS requests only. It opens no public listening ports.
Only locally configured services can be started, stopped, restarted, or have their
logs requested remotely. Configuration edits and Git pulls stay in the local UI.
Status and requested log contents are visible to your signed-in account; redact
credentials from your applications' logs before sharing them through the relay.

remote-agent.json contains a device credential. It is written with restrictive
permissions, excluded from Git, and must not be shared. Use **Disconnect computer**
in the hosted dashboard to revoke it. Stop the agent with Ctrl+C; projects remain
running. `python3 agent.py --disconnect` removes its local credential. No background
startup task is installed automatically in this preview.

Remote actions expire after 30 seconds without delivery. Delivered actions are
not replayed after connection loss; check actual project status before retrying
an uncertain action. The local dashboard remains usable if hosting is asleep.
