"""Shared matplotlib style: reference categorical slots 1-3 for x/y/z, recessive grid, neutral ink."""
C1, C2, C3 = "#2a78d6", "#eb6834", "#1baf7a"  # blue, orange, aqua (validated all-pairs as a trio)
AXES3 = (C1, C2, C3)
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e4e3df", "#fcfcfb"
SHADE = "#d9d8d3"  # neutral band (stationary segments)


def apply_style() -> None:
    import matplotlib as mpl
    mpl.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": GRID, "axes.labelcolor": INK2, "axes.titlecolor": INK,
        "axes.titlesize": 11, "axes.titlelocation": "left", "axes.titleweight": "bold", "axes.labelsize": 9,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
        "axes.spines.top": False, "axes.spines.right": False,
        "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelsize": 8, "ytick.labelsize": 8,
        "legend.fontsize": 8, "legend.labelcolor": INK2, "lines.linewidth": 1.2,
        "font.family": "sans-serif",
    })


def legend_above(ax, ncol: int = 4) -> None:
    """Legend in the header row, right-aligned, so it never covers data."""
    ax.legend(loc="lower right", bbox_to_anchor=(1.0, 1.0), ncol=ncol, frameon=False, borderaxespad=0.2)
