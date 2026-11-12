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
2. **Check the proxy.** Run `check_proxy.bat`. It tries both ways of using Code Interpreter with a tiny made-up workbook and prints PASS or FAIL for each step, then a 4-line summary.
   - **Part A** is the way the agent works today: the agent creates the sandbox, uploads into it, downloads from it and deletes it.
   - **Part B** is the automatic way: OpenAI creates the sandbox on the first code call, files go in through the Files API, and come out by download or, failing that, as text the code prints.
   - If Part A step 1 fails with 403, the proxy blocks sandbox creation. Copying `sandbox_settings.example.json` to `sandbox_settings.json` and setting `"provider"` (`"openai"` or `"azure"`) or `"model_on_create": true` sometimes helps. Otherwise the proxy admin can allow LiteLLM's `/v1/containers` routes, or the agent can switch to the automatic way if Part B passes.
   - Send back the whole output. Part B decides what the agent needs if Part A stays blocked.
   - If a step fails with "container not found", the model alias and the address are in different regions. The EMEA address works with the `openai.eu.gpt-6-luna` alias.
3. **Test the agent.** Run `run_test.bat`. It builds the six synthetic test packets once, runs the agent on the payroll packet through the proxy and scores the result. It usually takes 5 minutes, and up to 15 when OpenAI is slow. Name another packet to try it: `run_test.bat payment_journal`. While code runs you see `... running code in the sandbox` lines.
4. **The full scorecard** (Phase 7, about an hour, paid): `run_scorecard.bat`. It runs every packet and writes a comparison table under `runs\experiments\`. Send back its `summary.md`.
5. **Your own files** (only once approved for this use): `run_agent.bat path\to\packet.zip path\to\template.xlsx "What the run should produce"`. If the agent asks a question, type the answer in the window.

Each script waits for a key press before closing, so you can read or copy its output.
- Every run is saved under `runs\<run id>\`. The README says what each file holds.
- Close the output workbook in Excel before the agent saves again. Otherwise the save fails with a message saying so.
- Keep this folder out of OneDrive or any other synced folder: `runs\` will hold payroll outputs once real files are used.

To use a different proxy or model for the prototype only, put `"base_url"` or `"model"` in `sandbox_settings.json`. To use a different key, set `PROXY_API_KEY`.

## What to send back

- The whole output of `check_proxy.bat`, including the summary.
- The last lines of `run_test.bat`: the status and the score block.
- `summary.md` from `run_scorecard.bat`, when you run it.

None of these contain payroll data.
