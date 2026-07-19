# Career CV Repository

This repository manages multiple tailored CVs through a structured folder hierarchy.

Rather than maintaining each CV variant in a separate Git branch, all active CVs are stored in the same branch and organized by career category. Each folder represents a distinct professional positioning, while Git commits preserve the history of every version.

Each CV category contains:

- `main.tex` — the active LaTeX CV for that career positioning.
- `README.md` — documentation describing the category, target roles, positioning strategy, and relationship to other CV variants.

## Repository Structure

```text
.
├── Engineering/
│   ├── Embedded/
│   │   ├── README.md
│   │   └── main.tex
│   │
│   ├── QA/
│   │   ├── README.md
│   │   └── main.tex
│   │
│   ├── Software Engineering/
│   │   ├── README.md
│   │   └── main.tex
│   │
│   ├── System Engineering/
│   │   ├── README.md
│   │   └── main.tex
│   │
│   ├── Wireless/
│   │   ├── 5G-RAN/
│   │   │   ├── README.md
│   │   │   └── main.tex
│   │   │
│   │   ├── Wireless-Systems/
│   │   │   ├── RF Testing/
│   │   │   │   ├── README.md
│   │   │   │   └── main.tex
│   │   │   │
│   │   │   └── README.md
│   │   │
│   │   └── README.md
│   │
│   └── README.md
│
├── MasterCV/
│   ├── README.md
│   └── main.tex
│
├── Non-Engineering/
│   ├── Technical Sales/
│   │   ├── README.md
│   │   └── main.tex
│   │
│   └── README.md
│
└── README.md
