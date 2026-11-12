# Running on the Windows enterprise machine

Use this when the LiteLLM proxy is only reachable from inside the company network.

## What you need

- Python 3.12 or newer, from the Python launcher or on PATH (the same as the main app).
- The project's `.env` with `OPENAI_API_KEY` set to your proxy key. Or set `PROXY_API_KEY` if the prototype needs a different key.
- Your proxy address (it usually ends in `/v1`) and the model alias it uses.

Company certificates are read from the Windows certificate store, the same way as the main app. Proxy variables such as `HTTPS_PROXY`, and pip's `PIP_INDEX_URL` and `PIP_CERT`, are respected.

## Steps

1. **Set up once.** Double-click `setup.bat`, or run it in Command Prompt from this folder. It creates `.venv`, installs the packages, creates `sandbox_settings.json` and opens it in Notepad.
2. **Fill in the settings.** Put in `base_url` (your proxy) and `model` (your alias), then save. Leave `provider` as `null` and `model_on_create` as `false` for now. No secrets go in this file.
3. **Check the proxy.** Run `check_proxy.bat`. It tests the 7 things the agent needs, using a tiny made-up workbook, and prints PASS or FAIL for each.
   - If step 1 fails, change `provider` to `"openai"` or `"azure"`, try again, then also set `model_on_create` to `true`.
   - If step 2 or 5 fails, the proxy is probably swapping Code Interpreter for its own sandbox (LiteLLM's "interception" mode), which cannot move files yet. Ask the proxy admin whether Code Interpreter is passed through to OpenAI or Azure.
4. **Test the agent.** Run `run_test.bat`. It builds the synthetic test packet, runs the agent through the proxy and scores the result. It takes about 4 minutes.
5. **Your own files** (only once approved for this use): `run_agent.bat path\to\packet.zip path\to\template.xlsx`.

Each script waits for a key press before closing, so you can read or copy its output. Every run is saved under `runs\<run id>\`. See the README for what each file holds. Close the output workbook in Excel before the agent saves again, or the save fails with a message saying so.

Keep this folder out of OneDrive or any other synced folder: `runs\` will hold payroll outputs once real files are used.

## What to send back

The PASS/FAIL lines from `check_proxy.bat`, plus the last lines of `run_test.bat`: the status and the score block. Nothing from these contains payroll data.
