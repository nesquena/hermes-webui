# Skills panel

The Skills panel lists the skills the active Hermes profile can actually load, and
mirrors what the Agent itself will load. That set has two sources:

- **Directory skills** — `SKILL.md` files under the profile's `skills/` tree.
- **Plugin-registered skills** — skills a Hermes Agent plugin registers through
  `ctx.register_skill()` at runtime (#7770).

Both appear in the panel, in the `/` slash-command autocomplete, and in the skill
detail view.

## Plugin-qualified skill names

A plugin-registered skill is addressed by its **qualified name**:

```
<plugin>:<skill>
```

for example `superpowers:brainstorming` or `my_plugin:my_skill`. The namespace is
the plugin's own name; the part after the colon is the skill name the plugin
registered.

The qualified name is the identity everywhere:

| Surface | Identifier |
| --- | --- |
| `GET /api/skills` row `name` | `my_plugin:my_skill` |
| `GET /api/skills/content?name=` | `my_plugin:my_skill` |
| Slash command inserted into the composer | `/my_plugin:my_skill` |
| `skills.disabled` entry in `config.yaml` | `my_plugin:my_skill` |

The qualified name is **not** slugified. Directory skills are slugified for the
picker (`My Skill` becomes `my-skill`), but a plugin id keeps its underscores and
colons verbatim, because that is the exact spelling the Agent resolves. A
hyphenated form (`my-plugin:my-skill`) does not resolve to anything.

## Availability rules

The panel only advertises a skill the Agent will actually load. A plugin row is
hidden or marked disabled when:

- **It is disabled for this profile.** The effective disabled set is the
  profile's `skills.disabled` list, or `skills.platform_disabled.webui` when
  that key is present. A disabled plugin skill is listed as disabled, is not
  offered by the slash pickers, and its content is refused.
- **It is not supported on this platform.** A skill whose frontmatter restricts
  it (for example `platforms: [windows]`) is not listed on other platforms,
  matching the Agent's own eligibility check.
- **It belongs to another profile.** Plugin discovery is scoped to the active
  request profile, so a named profile sees only the plugin skills its own
  plugins register.

## Editing and deleting plugin skills

Plugin-provided skills are **read-only in the panel**. They live in the plugin
package, not in the profile's local `skills/` tree, so the panel hides the
**Edit** and **Delete** controls for those rows, and the API refuses both
operations with HTTP 409 rather than writing a partial local copy that would
shadow the plugin's real skill and drop its linked files.

To change a plugin skill, change the plugin (or the `SKILL.md` it ships) and
reload the plugin registry.

## Toggling a plugin skill

A plugin skill can be enabled and disabled from the panel like any other skill.
Disabling writes the **qualified** name to the active profile's
`skills.disabled` list; enabling removes it. Other profiles' configuration is
untouched.
