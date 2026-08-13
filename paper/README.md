# Paper draft and its toolchain

`burstserve.tex` is the working draft. It compiles today, on this box,
with no missing packages:

```
cd paper && latexmk -pdf burstserve.tex     # 3 pages, rc=0, no warnings
```

It deliberately uses `article` rather than a submission class, so that a
compile failure is always a problem with the draft and never with the
environment. Switching to a venue class is a one-line change once that
class is installed and complete.

## Toolchain, as measured

TeX Live 2026 at `/data/zhuoxu/tools/texlive/2026`, user-writable, so
`tlmgr install` works without sudo. `pdflatex`, `xelatex`, `lualatex`,
`latexmk`, `bibtex` and `biber` are all present.

| package | status |
| --- | --- |
| `article`, `booktabs`, `microtype`, `hyperref`, `geometry`, `amsmath`, `xcolor`, `caption`, `listings` | present |
| `siunitx` | installed 2026-08-13 |
| `acmart` | class installed, **but its dependency `totpages.sty` is not** — a minimal `sigconf` document still fails |
| `IEEEtran` | installing |
| `llncs` | absent (ships with the Springer bundle, not CTAN's `texlive-latex-extra`) |

CTAN is reachable directly from this box (the TUNA mirror answers HTTP/2
200). `tlmgr` itself is slow on first use because it fetches the whole
package database, so install it in the background rather than under a
short timeout; two attempts here were killed mid-download and left
`acmart` present but incomplete, which is exactly how the `totpages`
failure above arose.

**The lesson, since it will recur:** an installed `.cls` does not mean a
working class. Compile a minimal document in the target class before
believing the environment is ready. Checking `kpsewhich acmart.cls`
reported success while `sigconf` could not build.

## What is drafted, and what is not

The draft carries the claims from `docs/claims-and-evidence.md` and
nothing else. Where that file says a claim is withdrawn or open, the
draft says so too — the 18.6% utilisation figure, the two-episode
transient, and the dependence of fairness on the accounting currency are
absent from the draft because they are absent from the evidence.

Not yet written: related work, and the bibliography. There are no
citations in the draft, so `bibtex`/`biber` are unexercised.
