# Theming

The window's colours come from a theme file, so Atelier can match any desktop. Nothing in Atelier knows about a
particular desktop: anything that can write a small JSON file can theme it, and the window follows changes live.

## The theme file

`$XDG_CONFIG_HOME/atelier/theme.json`, by default `~/.config/atelier/theme.json`. It is one flat JSON object:

```json
{
  "mode": "dark",
  "window": "#0e1016",
  "panel": "#14191f",
  "panelRaised": "#21252b",
  "accent": "#267fb5",
  "selection": "#314365",
  "viewportBackground": "#1b1f25"
}
```

- Every colour is an opaque `#rrggbb` string. Eight hex digits are refused on purpose: they mean `#rrggbbaa` to CSS
  and most theme tools but `#aarrggbb` to Qt, so a translucent value could never mean one thing to every writer.
  The window derives its translucent shades from the tokens itself.
- **A missing key** keeps its built-in value, so a file may set as few tokens as it likes.
- **An unknown key** is ignored, with a warning in the log.
- **An invalid file** (not JSON, not an object, or any value that is not valid) is refused with an error in the log,
  and the theme already showing stays. A half-written save never blanks the window.
- **No file** shows the built-in theme, and removing the file while the window is open goes back to it.

### Live reload

The window watches the file and its folder, and applies a change within a fraction of a second, without a restart.
Saving in place, saving by rename (writing a new file and moving it over the old one, the safe way to install a
generated file), and the file or the `atelier/` folder appearing later all count.

## Tokens

The built-in values are the dark theme the window ships with.

| Key                      | Built-in  | Colours                                                                     |
|--------------------------|-----------|-----------------------------------------------------------------------------|
| `mode`                   | `dark`    | `dark` or `light`: the colours Qt derives itself (scroll bars, native dialogs) |
| `window`                 | `#1b1e24` | The window behind every pane, the stage                                    |
| `panel`                  | `#20242c` | The panes: sources, context, the header, the gallery and filmstrip          |
| `panelRaised`            | `#272b35` | Raised surfaces: buttons, cards, tiles                                      |
| `field`                  | `#15171c` | Input fields; at 80% opacity, the overlays on the viewport                  |
| `text`                   | `#e4e7ee` | Text                                                                        |
| `textDim`                | `#9aa1b2` | Secondary text, hints, disabled controls                                    |
| `selectionText`          | `#ffffff` | Text on the selection colour                                                |
| `accent`                 | `#6aa8ff` | Focus, the shown render, links, a running worker                            |
| `border`                 | `#30353f` | Borders and separators                                                      |
| `selection`              | `#2d4a73` | Selected rows and tiles; at 20% opacity, the drop target on the references  |
| `statusDraft`            | `#8a909c` | A source in draft                                                           |
| `statusInReview`         | `#e0b341` | A source in review                                                          |
| `statusChangesRequested` | `#e0645a` | A source with changes requested                                             |
| `statusApproved`         | `#5cc285` | An approved source                                                          |
| `danger`                 | `#e0645a` | Failures: a failed conversion, turn or tool call, a denied approval         |
| `warning`                | `#e0b341` | Waiting on you: approvals, a worker starting                                |
| `success`                | `#5cc285` | Done: an allowed approval, a finished tool call, an idle worker             |
| `viewportBackground`     | `#2a2e36` | The 3D viewport's backdrop, and behind renders, compare and markup          |
| `viewportGrid`           | `#808080` | The ground grid: every tenth line at full strength, the rest at half        |
| `viewportAxisX`          | `#ff0000` | Unreal's X axis on the grid                                                 |
| `viewportAxisY`          | `#0000ff` | Unreal's Y axis on the grid                                                 |

What a model looks like is not themed: clay, the scale figure, collision hulls and the lighting stay fixed, so a
model reads the same in every theme. Neither are the markup colours, which are drawn into the feedback image.

## How the window uses it

`atelier.gui.theme` reads and validates the file (`ThemeTokens`, a pydantic model whose defaults are the built-in
theme) and watches it (`ThemeFile`, registered as the QML singleton `Atelier.ThemeFile`). `qml/Theme.qml` is the
only QML that reads it: it exposes each token, derives the translucent and blended shades, and holds the fixed scene
colours. Every other QML file takes its colours from `Theme`, and a test keeps colour literals out of them.
`Main.qml` feeds the tokens to the Qt Quick Controls palette, so the stock controls follow the theme too. The ground
grid is drawn by a shader (`qml/shaders/grid.frag`) in a backdrop view under the model's (`ViewportGrid.qml`),
because Qt's own infinite grid has fixed colours.

## Omarchy users

[Omarchy](https://omarchy.org) renders `~/.config/omarchy/themed/*.tpl` against each theme's palette into
`~/.local/state/omarchy/current/theme/`, then runs the scripts in `~/.config/omarchy/hooks/theme-set.d/`. A template
and a hook make Atelier follow every theme switch.

`~/.config/omarchy/themed/atelier.json.tpl`:

```json
{
  "mode": "{{ mode }}",
  "window": "{{ dark_background }}",
  "panel": "{{ background }}",
  "panelRaised": "{{ lighter_background }}",
  "field": "{{ darker_background }}",
  "text": "{{ foreground }}",
  "textDim": "{{ mix foreground background 35% }}",
  "selectionText": "{{ bright_foreground }}",
  "accent": "{{ accent }}",
  "border": "{{ mix lighter_background foreground 10% }}",
  "selection": "{{ selection }}",
  "statusDraft": "{{ dark_foreground }}",
  "statusInReview": "{{ yellow }}",
  "statusChangesRequested": "{{ red }}",
  "statusApproved": "{{ green }}",
  "danger": "{{ red }}",
  "warning": "{{ yellow }}",
  "success": "{{ green }}",
  "viewportBackground": "{{ mix background lighter_background 50% }}",
  "viewportGrid": "{{ mix background foreground 45% }}",
  "viewportAxisX": "{{ red }}",
  "viewportAxisY": "{{ blue }}"
}
```

`~/.config/omarchy/hooks/theme-set.d/atelier` (executable) checks the rendered file and installs it atomically, so
Atelier never reads it half written. `omarchy-toggle-enabled skip-atelier-theme-changes` turns the sync off, which
removes the file and puts Atelier back on its built-in theme.

```bash
#!/bin/bash

# omarchy:summary=Sync the generated Omarchy theme to Atelier
# omarchy:hidden=true

ATELIER_SOURCE_PATH="$HOME/.local/state/omarchy/current/theme/atelier.json"
ATELIER_THEME_PATH="${XDG_CONFIG_HOME:-$HOME/.config}/atelier/theme.json"

if omarchy-toggle-enabled skip-atelier-theme-changes; then
  rm -f "$ATELIER_THEME_PATH"
  exit 0
fi

[[ -f $ATELIER_SOURCE_PATH ]] || exit 0

mkdir -p "$(dirname "$ATELIER_THEME_PATH")"
tmp=$(mktemp "$ATELIER_THEME_PATH.XXXXXX")

# -e fails on empty input too, which plain jq accepts silently by printing nothing.
if jq -e '
  def color:
    if type == "string" and test("^#[0-9A-Fa-f]{6}$") then . else error("\(.) is not #rrggbb") end;

  if (.mode | IN("dark", "light")) then . else error("mode \(.mode) is not dark or light") end
  | with_entries(if .key == "mode" then . else .value |= color end)
' "$ATELIER_SOURCE_PATH" >"$tmp"; then
  mv "$tmp" "$ATELIER_THEME_PATH"
else
  rm -f "$tmp"
  echo "Atelier theme not updated: $ATELIER_SOURCE_PATH is empty or has an invalid value" >&2
  exit 1
fi
```

The next theme switch renders the template and runs the hook. To apply it to the current theme without switching,
re-select the current theme.
