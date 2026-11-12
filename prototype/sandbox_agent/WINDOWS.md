# Running on the Windows enterprise machine

Use this when the LiteLLM proxy is only reachable from inside the company network. The prototype is set up the same way as the main app and reads the same `.env`, so if the main app already works on this machine, the proxy is already configured.

## What you need

- Python 3.12 or newer, from the Python launcher or on PATH (as for the main app).
- The project's `.env`, with the same three values the main app uses:
  - `OPENAI_API_KEY`: your proxy key
  - `OPENAI_BASE_URL`: your proxy address, usually ending in `/v1`
  - `OPENAI_MODEL`: the model alias

  If `.env` does not exist yet, run the main app's `start.bat` once; it creates it.

Same as the main app:
- Company certificates are read from the Windows certificate store.
- Proxy variables such as `HTTPS_PROXY` are respected, as are pip's `PIP_INDEX_URL` and `PIP_CERT`.
- The address must use HTTPS. Plain HTTP is allowed only to this machine.

## Steps

1. **Set up once.** Double-click `setup.bat` in this folder. It creates the prototype's own Python environment, installs the packages, and then shows which proxy address and model it will use and where they came from.
2. **Check the proxy.** Run `check_proxy.bat`. It tests the 7 things the agent needs, using a tiny made-up workbook, and prints PASS or FAIL for each.
   - **Step 1 fails:** copy `sandbox_settings.example.json` to `sandbox_settings.json`. Set `"provider"` to `"openai"` or `"azure"`, whichever your proxy routes to, and try again. If it still fails, also set `"model_on_create"` to `true`.
   - **Step 2 or 5 fails:** the proxy is probably swapping Code Interpreter for its own sandbox (LiteLLM's "interception" mode), which cannot move files yet. Ask the proxy admin whether Code Interpreter is passed through to OpenAI or Azure.
3. **Test the agent.** Run `run_test.bat`. It builds the synthetic test packet, runs the agent through the proxy and scores the result. It usually takes 4 minutes, and up to 15 when OpenAI is slow. While code runs you see `... running code in the sandbox` lines.
4. **Your own files** (only once approved for this use): `run_agent.bat path\to\packet.zip path\to\template.xlsx`.

Each script waits for a key press before closing, so you can read or copy its output.
- Every run is saved under `runs\<run id>\`. The README says what each file holds.
- Close the output workbook in Excel before the agent saves again. Otherwise the save fails with a message saying so.
- Keep this folder out of OneDrive or any other synced folder: `runs\` will hold payroll outputs once real files are used.

To use a different proxy or model for the prototype only, put `"base_url"` or `"model"` in `sandbox_settings.json`. To use a different key, set `PROXY_API_KEY`.

## What to send back

- The PASS/FAIL lines from `check_proxy.bat`.
- The last lines of `run_test.bat`: the status and the score block.

None of these contain payroll data.
