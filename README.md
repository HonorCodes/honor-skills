# honor-skills

Agent skills I use day to day, written to work in any project. Each folder is one skill with a
`SKILL.md` that agents such as Claude Code load on demand.

| Skill | What it does |
| --- | --- |
| [visual-review](visual-review/) | Opens local screenshots and videos in a private browser page where you write notes and draw markup; the agent reads back your verdict. |

## Install

Copy or symlink a skill folder into your agent's skills folder, for example:

    git clone https://github.com/HonorCodes/honor-skills.git
    mkdir -p ~/.claude/skills
    ln -sfn "$PWD/honor-skills/visual-review" ~/.claude/skills/visual-review

On Windows, copy the `visual-review` folder into `%USERPROFILE%\.claude\skills\`.

## Requirements

`visual-review` needs Python 3.9 or later and a current Chromium-based browser, Firefox or
Safari. It has no other dependencies. Development tests also need Node.js.

## License

MIT. See [LICENSE](LICENSE).
