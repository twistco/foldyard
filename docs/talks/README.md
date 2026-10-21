# Talks

Slide decks written in [Marp](https://marp.app/) Markdown: one file per deck, slides separated by
`---`, with the diagrams taken straight from [`../assets/`](../assets/) so the slides and the docs
never disagree.

| Deck | For | Length |
| --- | --- | --- |
| [why-sandbox.md](./why-sandbox.md) | Opening a hands-on workshop. Why a developer laptop needs a sandbox, what a sandbox must get right, how to compare tools — then foldyard as one worked answer, and the workshop plan. Tool-agnostic for its first two thirds. | ~35 min, 37 slides |

Speaker notes are the HTML comments under each slide; Marp shows them in presenter view. The
incident figures quoted come from security write-ups that disagree with each other and get revised;
re-check them the week you give the talk (the sources are on the last content slide).

## Render

The theme is [`foldyard-theme.css`](./foldyard-theme.css), Marp's built-in Gaia restyled with the
diagrams' palette and type.

```bash
# live preview in the browser, reloading on save
npx @marp-team/marp-cli why-sandbox.md --theme-set foldyard-theme.css --html --preview

# an HTML file (keep ../assets/ beside it when sharing), PDF or PowerPoint
# (PDF/PPTX need a local Chrome, Edge or Firefox)
npx @marp-team/marp-cli why-sandbox.md --theme-set foldyard-theme.css --html --allow-local-files -o why-sandbox.html
npx @marp-team/marp-cli why-sandbox.md --theme-set foldyard-theme.css --html --allow-local-files --pdf
npx @marp-team/marp-cli why-sandbox.md --theme-set foldyard-theme.css --html --allow-local-files --pptx
```

In VS Code, the *Marp for VS Code* extension previews it once the theme is registered:

```jsonc
// .vscode/settings.json
"markdown.marp.themes": ["./docs/talks/foldyard-theme.css"]
```

## Slide kinds

| Directive | Slide |
| --- | --- |
| `<!-- _class: lead -->` | dark cover or closing slide |
| `<!-- _class: section -->` | dark part divider |
| `<!-- _class: diagram -->` | one diagram, as large as the slide allows |
| *(none)* | light content slide |

Inside content slides: `<div class="cols">` for two columns, `<div class="card">` for a bordered
callout, `<span class="kicker">` for a small label above a title, and `> …` for a serif pull quote.

The docs diagrams are drawn for reading on a screen, so the denser ones (the network, the
architecture) are small on a projector. Zoom in, or talk over them and leave them up as a
reference. The blast-radius and landscape diagrams are drawn at talk scale.
