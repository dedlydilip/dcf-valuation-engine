"""Formatting conventions for the Excel output.

Follows the colour convention every banking model uses, because the first thing a
reviewer does is click a cell to see whether it is a number someone typed or a number
the model worked out:

    blue    a hardcoded input, safe to change
    black   a formula computed on this sheet
    green   a link to another sheet

Values are stored at full precision and displayed in millions through the number
format, so the arithmetic stays exact while the sheet stays readable.
"""

from __future__ import annotations

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

# Colours
NAVY = "1F3864"
LIGHT = "D9E2F3"
BLUE_INPUT = "0000FF"
GREEN_LINK = "007A33"
BLACK = "000000"
GREY = "808080"
RED = "C00000"

# Fonts
TITLE_FONT = Font(name="Calibri", size=14, bold=True, color="FFFFFF")
SECTION_FONT = Font(name="Calibri", size=11, bold=True, color=NAVY)
LABEL_FONT = Font(name="Calibri", size=10)
INPUT_FONT = Font(name="Calibri", size=10, color=BLUE_INPUT)
FORMULA_FONT = Font(name="Calibri", size=10, color=BLACK)
LINK_FONT = Font(name="Calibri", size=10, color=GREEN_LINK)
TOTAL_FONT = Font(name="Calibri", size=10, bold=True)
NOTE_FONT = Font(name="Calibri", size=9, italic=True, color=GREY)
WARN_FONT = Font(name="Calibri", size=9, italic=True, color=RED)
HEADER_FONT = Font(name="Calibri", size=10, bold=True, color="FFFFFF")

# Fills
TITLE_FILL = PatternFill("solid", fgColor=NAVY)
HEADER_FILL = PatternFill("solid", fgColor=NAVY)
SECTION_FILL = PatternFill("solid", fgColor=LIGHT)

# Borders
_thin = Side(style="thin", color=GREY)
TOP_BORDER = Border(top=_thin)
BOTTOM_BORDER = Border(bottom=_thin)
TOTAL_BORDER = Border(top=_thin, bottom=Side(style="double", color=BLACK))

RIGHT = Alignment(horizontal="right")
CENTER = Alignment(horizontal="center")
LEFT_INDENT = Alignment(horizontal="left", indent=1)
WRAP = Alignment(wrap_text=True, vertical="top")

# Number formats. The double comma displays a value in millions without altering it.
MONEY_M = '#,##0;(#,##0)'
MONEY_MM = '#,##0,,;(#,##0,,)'
MONEY_RAW = '#,##0;(#,##0)'
PRICE = '$#,##0.00;($#,##0.00)'
PERCENT = '0.0%;(0.0%)'
PERCENT_2 = '0.00%;(0.00%)'
MULTIPLE = '0.0"x"'
SHARES_MM = '#,##0.0,,;(#,##0.0,,)'
RATIO = '0.00'
INTEGER = '#,##0'


def col_width(worksheet, widths: dict[str, float]) -> None:
    for column, width in widths.items():
        worksheet.column_dimensions[column].width = width
