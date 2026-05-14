"""Generate a PowerPoint sales report for a company."""

import os
import tempfile
from datetime import datetime

from pptx import Presentation
from pptx.chart.data import ChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.util import Inches, Pt

import database as db

MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

PRIMARY   = RGBColor(0x1A, 0x56, 0xDB)   # blue
DARK      = RGBColor(0x11, 0x18, 0x27)   # near-black
LIGHT_BG  = RGBColor(0xF3, 0xF4, 0xF6)  # light grey


def _set_font(run, size=None, bold=False, color=None):
    run.font.bold = bold
    if size:
        run.font.size = Pt(size)
    if color:
        run.font.color.rgb = color


def _title_slide(prs, company_name, year_label, brand, industry):
    layout = prs.slide_layouts[6]  # blank
    slide = prs.slides.add_slide(layout)
    w, h = prs.slide_width, prs.slide_height

    # Background
    bg = slide.shapes.add_shape(1, 0, 0, w, h)
    bg.fill.solid()
    bg.fill.fore_color.rgb = PRIMARY
    bg.line.fill.background()

    # Company name
    tb = slide.shapes.add_textbox(Inches(0.8), Inches(2.2), Inches(8.4), Inches(1.2))
    tf = tb.text_frame
    tf.word_wrap = False
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = company_name.upper()
    _set_font(run, size=36, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF))

    # Subtitle
    tb2 = slide.shapes.add_textbox(Inches(0.8), Inches(3.5), Inches(8.4), Inches(0.6))
    tf2 = tb2.text_frame
    p2 = tf2.paragraphs[0]
    run2 = p2.add_run()
    run2.text = f"Sales Report  ·  {year_label}"
    _set_font(run2, size=18, color=RGBColor(0xBF, 0xDB, 0xFF))

    # Industry tag
    if brand or industry:
        tag = f"{brand}  |  {industry}" if brand and industry else (brand or industry)
        tb3 = slide.shapes.add_textbox(Inches(0.8), Inches(4.3), Inches(8.4), Inches(0.5))
        tf3 = tb3.text_frame
        p3 = tf3.paragraphs[0]
        run3 = p3.add_run()
        run3.text = tag
        _set_font(run3, size=13, color=RGBColor(0xBF, 0xDB, 0xFF))

    # Date generated
    tb4 = slide.shapes.add_textbox(Inches(0.8), Inches(6.5), Inches(8.4), Inches(0.4))
    tf4 = tb4.text_frame
    p4 = tf4.paragraphs[0]
    run4 = p4.add_run()
    run4.text = f"Generated: {datetime.today().strftime('%d %B %Y')}"
    _set_font(run4, size=11, color=RGBColor(0xBF, 0xDB, 0xFF))


def _chart_slide(prs, title, months, actuals, targets):
    layout = prs.slide_layouts[6]
    slide = prs.slides.add_slide(layout)
    w, h = prs.slide_width, prs.slide_height

    # Slide title
    tb = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(9), Inches(0.6))
    p = tb.text_frame.paragraphs[0]
    run = p.add_run()
    run.text = title
    _set_font(run, size=20, bold=True, color=DARK)

    # Bar chart
    chart_data = ChartData()
    chart_data.categories = months
    chart_data.add_series("Actual", actuals)
    if any(t for t in targets if t):
        chart_data.add_series("Target", targets)

    chart = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED,
        Inches(0.4), Inches(1.0), Inches(9.2), Inches(5.5),
        chart_data,
    ).chart
    chart.has_legend = bool(any(t for t in targets if t))
    chart.has_title = False
    chart.plots[0].series[0].format.fill.solid()
    chart.plots[0].series[0].format.fill.fore_color.rgb = PRIMARY

    return slide


def _summary_slide(prs, months, actuals, targets):
    layout = prs.slide_layouts[6]
    slide = prs.slides.add_slide(layout)

    tb = slide.shapes.add_textbox(Inches(0.5), Inches(0.3), Inches(9), Inches(0.6))
    p = tb.text_frame.paragraphs[0]
    run = p.add_run()
    run.text = "Monthly Breakdown"
    _set_font(run, size=20, bold=True, color=DARK)

    cols = 4
    col_w = [Inches(1.2), Inches(2.5), Inches(2.5), Inches(2.5)]
    headers = ["Month", "Actual (MYR)", "Target (MYR)", "vs Target"]

    rows = len(months) + 1
    table = slide.shapes.add_table(rows, cols,
                                   Inches(0.4), Inches(1.0),
                                   Inches(9.2), Inches(min(5.5, rows * 0.38))).table

    for c, (w, h) in enumerate(zip(col_w, headers)):
        table.columns[c].width = w
        cell = table.cell(0, c)
        cell.fill.solid()
        cell.fill.fore_color.rgb = PRIMARY
        p = cell.text_frame.paragraphs[0]
        run = p.add_run()
        run.text = h
        _set_font(run, size=11, bold=True, color=RGBColor(0xFF, 0xFF, 0xFF))

    for r, (month, actual, target) in enumerate(zip(months, actuals, targets), start=1):
        pct = ""
        if target and actual is not None:
            pct = f"{((actual - target) / target * 100):+.1f}%"
        row_data = [
            month,
            f"{actual:,.2f}" if actual is not None else "-",
            f"{target:,.2f}" if target else "-",
            pct,
        ]
        for c, val in enumerate(row_data):
            cell = table.cell(r, c)
            if r % 2 == 0:
                cell.fill.solid()
                cell.fill.fore_color.rgb = LIGHT_BG
            p = cell.text_frame.paragraphs[0]
            run = p.add_run()
            run.text = val
            _set_font(run, size=10, color=DARK)


def create_sales_slides(company_name: str, year: int = None) -> str:
    """Build a .pptx sales report and return the file path."""
    company_list = db.get_company_details(company_name)
    if not company_list:
        raise ValueError(f"Company '{company_name}' not found.")
    company = company_list[0]

    sales = db.get_sales_summary(company_name, year=year)
    if not sales:
        raise ValueError(f"No sales data found for '{company_name}'.")

    actual_map = {}
    target_map = {}
    for row in sales:
        if row.get("year") is None or row.get("month") is None:
            continue
        key = (int(row["year"]), int(row["month"]))
        if row["type"] == "actual":
            actual_map[key] = row["amount"]
        elif row["type"] == "target":
            target_map[key] = row["amount"]

    # Sort keys
    keys = sorted(set(actual_map) | set(target_map))
    months   = [f"{MONTH_NAMES[k[1]-1]} {k[0]}" for k in keys]
    actuals  = [actual_map.get(k) for k in keys]
    targets  = [target_map.get(k) for k in keys]

    year_label = str(year) if year else f"{keys[0][0]}–{keys[-1][0]}" if keys else "All Years"

    prs = Presentation()
    prs.slide_width  = Inches(10)
    prs.slide_height = Inches(7.5)

    _title_slide(prs, company["company_name"], year_label,
                 company.get("brand", ""), company.get("industry", ""))
    _chart_slide(prs, f"Monthly Sales — {year_label}", months, actuals, targets)
    _summary_slide(prs, months, actuals, targets)

    safe_name = company["company_name"].replace(" ", "_").replace("/", "-")
    filename = f"{safe_name}_Sales_{year or 'All'}.pptx"
    path = os.path.join(tempfile.gettempdir(), filename)
    prs.save(path)
    return path
