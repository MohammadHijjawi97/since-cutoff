# The Python AI stack, by model

`requirements.txt` pins 36 widely used Python AI and LLM libraries at their releases of
26 September 2026. Scanning it against a model's training cutoff shows how much of the stack
changed after the model's training data ends. The results for 21 models from 8 vendors are on
[What your model hasn't seen](https://mohammadhijjawi97.github.io/since-cutoff/ai-stack.html).

## Reproduce

No API key is needed: `scan` makes no model calls. The first scan downloads the sources of each
library at both versions, which takes a few minutes; later scans reuse the cache.

```bash
pip install since-cutoff
cd examples/ai-stack
mkdir -p scans
since-cutoff scan --model openai:gpt-5 --json > scans/gpt-5.json
since-cutoff scan --model anthropic:claude-opus-5-5 --json > scans/claude-opus-5-5.json
since-cutoff scan --model openrouter:google/gemini-2.5-pro --json > scans/gemini-2.5-pro.json
python stack_report.py scans      # writes stack.md, stack.json and the two chart SVGs
```

`since-cutoff models` lists every model with a known training cutoff. For a model it does not
know, pass the date instead: `--cutoff 2025-06`.
