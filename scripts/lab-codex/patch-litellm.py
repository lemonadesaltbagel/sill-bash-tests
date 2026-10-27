"""Patches the installed LiteLLM's Gemini-API adapter for the Gemini CLI runs.

LiteLLM turns Gemini's functionCall/functionResponse parts into OpenAI tool calls with the
id `call_<function name>`, so two calls to one tool in a turn (Gemini CLI's parallel
read_file calls) share an id, and DeepSeek rejects the conversation ("Duplicate value for
'tool_call_id'"). This uses the part's own id when it has one, and otherwise numbers each
function's calls in order, pairing the n-th response with the n-th call.

It also turns DeepSeek's thinking off for these requests: on by default, it wants each
turn's reasoning sent back, which the Gemini format can't carry (400 "reasoning_content
... must be passed back"), and the adapter drops `thinking` from the proxy config.
Idempotent.
"""
import os
import litellm

path = os.path.join(os.path.dirname(litellm.__file__), "google_genai", "adapters", "transformation.py")
src = open(path, encoding="utf-8").read()
already = "_sill_id(" in src

if already:
    print("already patched:", path)
else:
    old_resp = "tool_call_id=f\"call_{func_response.get('name', 'unknown')}\","
    old_call = "id=f\"call_{func_call.get('name', 'unknown')}\","
    old_loop = "        for content in contents:\n            role = content.get(\"role\", \"user\")"
    for needle in (old_resp, old_call, old_loop):
        if src.count(needle) != 1:
            raise SystemExit(f"unexpected LiteLLM source; not patching ({needle[:40]})")

    src = src.replace(old_loop, (
        "        # sill e2e patch: unique tool call ids (see scripts/lab-codex/patch-litellm.py)\n"
        "        _calls_seen: dict[str, int] = {}\n"
        "        _responses_seen: dict[str, int] = {}\n"
        "\n"
        "        def _sill_id(part: Any, seen: dict[str, int]) -> str:\n"
        "            name = part.get('name', 'unknown')\n"
        "            seen[name] = seen.get(name, 0) + 1\n"
        "            return part.get('id') or f\"call_{name}_{seen[name]}\"\n"
        "\n" + old_loop))
    src = src.replace(old_resp, "tool_call_id=_sill_id(func_response, _responses_seen),")
    src = src.replace(old_call, "id=_sill_id(func_call, _calls_seen),")
    open(path, "w", encoding="utf-8").write(src)
    print("patched:", path)

handler = os.path.join(os.path.dirname(path), "handler.py")
src = open(handler, encoding="utf-8").read()
ret = "        return GenerateContentCompletionKwargs(**completion_kwargs)"
if '"thinking": {"type": "disabled"}' in src:
    print("already patched:", handler)
else:
    if src.count(ret) != 1:
        raise SystemExit("unexpected LiteLLM handler source; not patching")
    src = src.replace(ret, (
        "        # sill e2e patch: no thinking (see scripts/lab-codex/patch-litellm.py)\n"
        "        completion_kwargs[\"extra_body\"] = {**(completion_kwargs.get(\"extra_body\") or {}),\n"
        "                                         \"thinking\": {\"type\": \"disabled\"}}\n" + ret))
    open(handler, "w", encoding="utf-8").write(src)
    print("patched:", handler)
