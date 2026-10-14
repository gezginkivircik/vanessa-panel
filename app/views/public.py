"""Müşteri web sitesi. Faz 12'de tam site + online randevu eklenecek.
Şimdilik yalnızca gerçek çalışan iletişim bilgileri olan sade bir karşılama sayfası."""
from flask import Blueprint, render_template

bp = Blueprint("public", __name__)


@bp.get("/")
def home():
    return render_template("public/home.html")
