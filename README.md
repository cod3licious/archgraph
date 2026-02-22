# ArchGraph

ArchGraph turns a plain-text description of your codebase architecture into an interactive dependency graph. You describe your modules, submodules, and units (functions, classes, etc.) in two files — a layer hierarchy in JSON and unit descriptions in Markdown — and ArchGraph processes them into a single `result.json` that a frontend can render as a navigable graph with layer-violation highlighting.

**You can find an example visualization of a fictional e-commerce codebase [here](https://franziskahorn.de/demo_archgraph/) — click around!** \
This is based on the descriptions in `example_data/` with 5 modules, 19 submodules, 115 units, and a few intentional layer violations.

While other tools exist to visualize codebases written in a specific programming language, **since ArchGraph operates on plain text descriptions, it is language-agnostic** and can also be used to **visualize your design docs** - which is even more helpful in the era of AI, where you plan more and code less.

## How it works

1. **`layers.json`** — defines the module hierarchy and their allowed dependency directions as a two-level layer structure (root modules → submodules). Dependencies must flow strictly downward through the layers; anything that points back up is flagged as a violation.

2. **`units.md`** — describes every unit (function, class, method) in plain prose. Dependencies are declared inline using backtick-at notation: `` `@submodule.UnitName` ``. ArchGraph extracts these automatically.

3. **`prepare.py`** — reads both files, resolves all dependencies, validates them against the layer hierarchy, and writes `result.json`.

4. **Frontend** — reads `result.json` and renders the interactive graph in the browser.

### Project structure

```
archgraph/
├── example_data/
│   ├── layers.json       # example e-commerce layer hierarchy
│   └── units.md          # example e-commerce unit descriptions
└── src/
    ├── prepare.py        # data processing pipeline
    ├── test_prepare.py   # unit tests
    ├── result.json       # output of prepare.py (read by the frontend)
    └── index.html        # interactive graph visualization
```

### Running the data pipeline

Requires [uv](https://docs.astral.sh/uv/).

**Process a folder** containing `layers.json` and `units.md`:

```bash
uv run src/prepare.py --input example_data
```

**Process individual files:**

```bash
uv run src/prepare.py --layers path/to/layers.json --units path/to/units.md
```

Output is written to `src/result.json`.

**Run the tests:**

```bash
uv run pytest src/
```

### Running the frontend

Open `src/index.html` in a browser served by any static file server. It will read `result.json` from the same directory and render the graph.

```bash
# simple local server, no installation required
python -m http.server 8000 --directory src
# then open http://localhost:8000
```

## How I created this project with AI

I don't code a lot by hand these days, but I still care deeply about well-designed software. I follow my [Clarity-Driven Development approach](https://franziskahorn.de/articles/2026-01-cdd-humans-ai), where you first think through the Why, What, and How and capture it in sketch documents like [`sketch.md`](sketch.md) before writing a single line of code.

After some minor refinements based on Claude's feedback, I implemented the project step by step with Claude Agent in the [Zed IDE](https://zed.dev/) - tests first, then the Python script, then the frontend. The frontend in particular was a lot of fun: having AI in the loop made it easy to iterate on design ideas until we landed on something less cluttered than the typical arrow-heavy diagram. Instead of showing all connections at once, small icons on each box indicate incoming and outgoing dependencies, and the actual lines only appear when you click on a submodule or unit.

The whole implementation took one weekend: about 1.5 days writing the sketch document and a few hours instructing Claude (Sonnet 4.6). Without a pro plan it would have cost me around $12 in tokens - definitely worth it!


## Using ArchGraph with your own codebase

The two input files can describe any codebase. When designing a new software project, it always helps me to sketch out the individual functions and classes this way — and now I can also visualize the result to get a better feeling for what the final implementation would look like. 

If you want to visualize an existing project, instead of creating these documents manually, you can ask an AI agent to generate them by analyzing your codebase. But please note that, unlike a programmatic, deterministic code analyzer, the AI might miss things or get things wrong, so you should double check the results.

### File formats

**`layers.json`** — a two-level hierarchy. `root_layers` is a list of rows; within each row modules are siblings (no dependency allowed between them). `submodule_layers` optionally breaks each root module into its own sub-rows following the same rule.

```json
{
  "root_layers": [
    ["api"],
    ["services"],
    ["core"]
  ],
  "submodule_layers": {
    "services": [
      ["services.orders"],
      ["services.catalog"],
      ["services.payments"]
    ]
  }
}
```

**`units.md`** — one `### ` heading per unit, named `submodule.UnitName`. The body is a plain-English description. Declare dependencies inline with `` `@submodule.OtherUnit` ``.

```markdown
### services.orders.create_order
Creates a new order record. Calls `@services.catalog.get_product` to validate
line items and `@core.db.execute` to persist the order.
```

### AI prompt for generating input files from your codebase

Paste the following prompt into your AI agent of choice (Claude, GPT-4, Gemini, etc.) after giving it access to your repository:


> I want you to analyse this codebase and produce two files for a tool called ArchGraph.
>
> **File 1 — `layers.json`**
>
> Identify the top-level modules and how they depend on each other. Group them into a strict layered hierarchy where dependencies only flow downward (higher-level modules call lower-level ones, never the reverse). Represent this as a JSON object:
>
> ```json
> {
>   "root_layers": [
>     ["ModuleA"],
>     ["ModuleB", "ModuleC"],
>     ["ModuleD"]
>   ],
>   "submodule_layers": {
>     "ModuleB": [
>       ["ModuleB.sub1"],
>       ["ModuleB.sub2", "ModuleB.sub3"]
>     ]
>   }
> }
> ```
>
> Rules:
> - Each inner list is a *row*. Modules in the same row are siblings and must not depend on each other.
> - Row 0 is the top of the hierarchy (e.g. HTTP handlers, CLI entrypoints). The last row is the bottom (e.g. database, cache, shared utilities).
> - `submodule_layers` follows the same row structure within a single root module. Only include a module if it has meaningful internal sub-layers; omit it otherwise and it will be treated as a leaf.
> - Submodule names must be prefixed with their parent module name and a dot (e.g. `payments.gateway`).
>
> These layers should describe the **desired** dependency hierarchy; our existing codebase might violate these rules, so focus more on how things should be instead of the actual dependencies you find in the code.
>
> **File 2 — `units.md`**
>
> For every significant unit in the codebase (public function, class, or method worth documenting), write a `### ` section. The heading must be the full dot-path: `submodule.UnitName`. The body is one to three sentences describing what the unit does. Inline every dependency on another unit using backtick-at notation: `` `@submodule.OtherUnit` ``.
>
> ```markdown
> ### payments.gateway.charge
> Initiates a payment charge for the given amount and payment token. Calls
> `@core.db.execute` to store the charge record and `@core.events.publish`
> to emit a `payment.charged` event.
> ```
>
> Rules:
> - Only reference units that you have also described with their own `### ` heading.
> - Use the exact submodule path from `layers.json` as the prefix.
> - It is fine to omit purely internal helper units; focus on the public interface of each submodule.
> - Each unit heading must be exactly `submodule.UnitName` — two segments joined by the last dot. `services.ml.Model` is a valid heading (submodule `services.ml`, name `Model`). Do **not** write method names as headings (e.g. `services.ml.Model.predict` is wrong — it would be parsed as submodule `services.ml.Model`, which does not exist). If you want to reference a specific method as a dependency, use `` `@services.ml.Model.predict` `` in the body text; it will be resolved to the parent unit `services.ml.Model` automatically.
> - Do not invent dependencies that do not exist in the actual code.
>
> Produce both files in full. Think carefully about the layer ordering before writing `layers.json` — the most common mistake is placing a module too high when it is actually called by others.
