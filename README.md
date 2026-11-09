# claude-image-gen

A skill for Claude Code that makes and edits images for you, and keeps an eye on what they cost.

## What it is

You ask Claude Code for an image in plain words, and it makes one with **OpenAI GPT Image** or
**Google Nano Banana** (Gemini). It can create a new image, edit one you already have, put your real
product into a scene, or fix one part of a picture. Before it spends anything, it does a free
practice run and shows you the price. It can run several images in one batch, side by side. Each
file is saved exactly where you said, and every real run is written to a spend log, so you can
always check what you have actually spent.

## What you need

- **Claude Code on your own computer.** You need a paid Claude plan or an Anthropic Console
  account. See the [Claude Code overview](https://code.claude.com/docs/en/overview).
- **Python**, so the skill's scripts can run.
- **An API key with credit** for at least one image service:
  - OpenAI (GPT Image): <https://platform.openai.com/api-keys>
  - Google Gemini (Nano Banana): <https://aistudio.google.com/apikey>

It has been used on Windows. macOS and Linux have not been tested.

## Install

### The easy way: let Claude Code install it

Open Claude Code (in any folder) and paste this prompt. Claude does the installing and asks before
every step. Your API key never goes into the chat: you type it into your own terminal yourself.

```
I want to install the image-gen skill from https://github.com/a-moez/claude-image-gen

1. Read the repo's README.md first and follow its install steps.
2. Before installing anything, check what I already have: Python, pip,
   the skill's packages, and whether the skill is already in my Claude
   skills folder. Tell me what's there and what's missing.
3. Install only what's missing. Ask me before every install command,
   and explain each step in one simple sentence. If something can't be
   installed from here (like Python itself), tell me exactly how to do it.
4. Put the image-gen folder in my Claude skills folder.
5. Add the spend-confirmation hook from the README to my user settings.
   Show me the change before you save it.
6. Do NOT ask me for my API key in this chat. When it's time, tell me
   the exact command to run setup_key.py in my own terminal, and wait
   until I say I'm done.
7. Finish by running the skill's free check (imagegen.py --check), tell
   me in plain words if everything is ready, and tell me if I need to
   restart Claude Code for the skill or the hook to load.
```

You need an API key with credit first (see "What you need" above). If you're on macOS or Linux, the
skill hasn't been tested there yet.

### By hand

1. **Download this repo.** Click *Code → Download ZIP* and unzip it, or clone it with git.
2. **Copy the `image-gen` folder** into your Claude skills folder, `~/.claude/skills/`.
   On Windows that is `C:\Users\<you>\.claude\skills\`, so you end up with
   `C:\Users\<you>\.claude\skills\image-gen\SKILL.md`.
3. **Install what the scripts need.** In a terminal, from the folder you downloaded:
   ```
   pip install -r image-gen/scripts/requirements.txt
   ```
   You only need the library for the service you use (`openai` or `google-genai`). Pillow is
   the one thing every setup needs.
4. **Store your keys once.** Go into the skill's `scripts` folder and run:
   ```
   python setup_key.py
   ```
   It asks for each key and hides what you type. Keys are saved in
   `~/.config/claude-imagegen/` and work from every project on your computer.
5. **Check it works.** This is free:
   ```
   python imagegen.py --check
   ```
   It shows which keys it found (only the last few characters) and the library versions.

## The spend-confirmation hook (recommended)

A hook is a small check that Claude Code runs before a command. This one makes Claude Code stop and
ask you to confirm before **any real, paid image run**. Free dry runs pass through silently.

Open `~/.claude/settings.json` and add this entry inside `hooks` → `PreToolUse` (create those
keys if they aren't there yet). Replace `<you>` with your user name, or use the full path to
wherever you put the skill:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "python \"C:/Users/<you>/.claude/skills/image-gen/scripts/spend_gate.py\"",
            "statusMessage": "Checking image-gen spend gate"
          }
        ]
      }
    ]
  }
}
```

Put it in your user settings, not a project's, so it protects every project.

## Using it

You don't need to learn any commands. Just ask Claude in plain words. For example:

- *"Make a product shot of this mug on a wooden table, 4:5."*
- *"Edit this photo: put my real candle (candle.jpg) on that shelf instead of the vase."*
- *"In this image, change only the background to a soft beige wall."*
- *"Make three versions of this banner: square, 4:5 and 16:9."*

What happens next:

1. Claude writes the prompt and does a **free dry run**, which prices the job.
2. It shows you the model, size, number of images and **estimated cost**, and asks before spending.
3. You say yes, it runs, and it tells you where each file was saved.

If you ask for several images, Claude groups them into **one batch**, so you approve once.

## The money safety

- **Dry run first.** `--dry-run` prints the full request and the price, and spends nothing.
- **Ask before spending.** The skill tells Claude to get your OK, with the estimated total, before
  any real run.
- **An 8-image cap.** The script refuses more than 8 images in one run unless forced.
- **The hook** above makes Claude Code ask for confirmation on every real run.
- **A spend log.** Every real run is recorded in one log for your whole computer, tagged with the
  project folder it ran from and, if you give one, a `--project` name.
- **`--usage`** shows your totals, broken down by project and by folder.
- **`--calibrate`** shows what each image really cost, as measured from the provider's own figures.

**The default quality is `high`,** because the skill is built for work where detail matters. For
drafts, ask for `medium`, which is roughly 4× cheaper, or `low`.

Gemini charges a flat price per image, so its estimate is the final number. OpenAI charges by
usage, so the real cost is only known after the run; it is logged then. On GPT Image 2.5 at
`low` and `medium` quality, the estimate counts your prompt and reference photos too, and has
landed within 15% of the real cost on most runs. For OpenAI's `xhigh` and
`max` quality, no price is published, so the estimate shows `~$?` instead of a guess.

## Which model for what

Claude picks the model, but you can ask for one.

| Job | Model | Why |
|---|---|---|
| Everyday high-quality images | `flare` | OpenAI's fast default |
| Precise editing, premium work | `sunburst` | OpenAI, built for editing precision; slower |
| Cheap OpenAI | `mini` | `gpt-image-1-mini` |
| Keeping a real product exactly as it is | `pro` | Gemini, strong at keeping a real object faithful |
| Cheap volume on Gemini | `flash` / `lite` | the workhorse and the cheapest |

A rough rule: OpenAI is better at layouts and text on the image. Gemini keeps a real product more
faithful and is usually cheaper.

## Command reference

You won't usually type these; Claude does. Run them from the skill's `scripts` folder.

These never spend money:

```bash
python imagegen.py --usage [--project slug] [--repo name]   # totals across every repo
python imagegen.py --calibrate     # measured $/image from real API usage
python imagegen.py --check         # keys + SDK versions
python imagegen.py --list-models   # every model name you can use
```

A few direct examples:

```bash
# price it for free, first — always
python imagegen.py --model flare --prompt-file p.txt -o out.png --dry-run

# generate
python imagegen.py --model flare --prompt-file p.txt --aspect 4:5 --res 2K -o out.png --project acme

# edit / swap: give it the image(s) to work from with --ref
python imagegen.py --model sunburst --prompt-file swap.txt \
    --ref scene-v1.png --ref real-product.jpg -o scene-v2.png

# several images, one approval
python imagegen.py --batch runs.json
```

`--aspect` (like `4:5` or `16:9`) and `--res` (`1K`, `2K`, `4K`) mean the same thing on both
services. The tool never overwrites a file unless you add `--overwrite`.

## Troubleshooting

| What you see | What it means |
|---|---|
| `a network proxy refused it` | **Not a bad key.** A sandbox (e.g. Claude Cowork) is blocking the service. Run the command in your own terminal. |
| `OpenAI rejected the credential` | The key really is wrong. |
| `accepted the key but refused the request` | Billing or model access, not the key. |
| `openai 1.x is too old` | Run `pip install -U "openai>=2"`. |
| `not installed for this interpreter` | A project's own Python environment is active; the error prints the exact `pip install -r` line to run. |

Image models change fast. If a model name is rejected, check the provider's current docs.

## More detail

- `image-gen/SKILL.md`: the full instructions Claude follows.
- `image-gen/reference/openai-images.md` and `gemini-images.md`: measured behaviour of each service.
- `image-gen/reference/architecture.md`: how the tool is built, for anyone changing it.

## Licence

MIT. See [LICENSE](LICENSE).
