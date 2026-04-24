"""
utils/graph_client.py — Microsoft Graph email notifications
Reuses the MSAL Application credential pattern from six50_ai_coo.py
"""

import os
import json
import logging
import requests
from msal import ConfidentialClientApplication

logger = logging.getLogger(__name__)

TENANT_ID    = os.environ["AZURE_TENANT_ID"]
CLIENT_ID    = os.environ["AZURE_CLIENT_ID"]
CLIENT_SECRET = os.environ["AZURE_CLIENT_SECRET"]
SENDER_EMAIL  = os.environ["NOTIFY_FROM_EMAIL"]   # The mailbox sending alerts
NOTIFY_EMAIL  = os.environ["NOTIFY_TO_EMAIL"]      # Where alerts are sent (can be same)

SCOPES = ["https://graph.microsoft.com/.default"]


def _get_token() -> str:
    app = ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=SCOPES)
    if "access_token" not in result:
        raise RuntimeError(f"MSAL token error: {result.get('error_description')}")
    return result["access_token"]


def send_alert_email(subject: str, html_body: str) -> bool:
    token = _get_token()
    url = f"https://graph.microsoft.com/v1.0/users/{SENDER_EMAIL}/sendMail"

    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [
                {"emailAddress": {"address": NOTIFY_EMAIL}}
            ],
        },
        "saveToSentItems": "false",
    }

    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )

    if resp.status_code == 202:
        logger.info(f"Alert email sent: {subject}")
        return True
    else:
        logger.error(f"Graph send failed {resp.status_code}: {resp.text}")
        return False


# ── Email Templates ────────────────────────────────────────────────────────────

def build_hotel_alert_html(alerts: list[dict]) -> str:
    rows = ""
    for a in alerts:
        arrow = "🔻" if a.get("is_new_low") else "⚠️"
        prev = f"${a['prev_low']:.0f}" if a.get("prev_low") else "—"
        rows += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0">{arrow} <b>{a['hotel_name']}</b></td>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0">{a['check_in']} → {a['check_out']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0">{a['provider']}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#22c55e"><b>${a['price']:.0f}</b></td>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#94a3b8">{prev}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0">{a.get('alert_type','').replace('_',' ').title()}</td>
        </tr>"""

    return f"""
    <html><body style="font-family:system-ui,sans-serif;color:#1e293b;max-width:700px;margin:0 auto">
      <div style="background:#0f172a;color:white;padding:20px 24px;border-radius:8px 8px 0 0">
        <h2 style="margin:0;font-size:18px">✈️ Travel Tracker — Price Alert</h2>
        <p style="margin:4px 0 0;opacity:.7;font-size:13px">{len(alerts)} deal(s) found this run</p>
      </div>
      <div style="border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px;overflow:hidden">
        <table style="width:100%;border-collapse:collapse;font-size:14px">
          <thead>
            <tr style="background:#f8fafc">
              <th style="padding:10px 12px;text-align:left;font-weight:600">Hotel</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600">Dates</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600">Provider</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600">Price</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600">Prev Low</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600">Type</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <p style="font-size:11px;color:#94a3b8;margin-top:12px">Six50 Travel Tracker · Auto-generated</p>
    </body></html>"""


def build_flight_alert_html(alerts: list[dict]) -> str:
    rows = ""
    for a in alerts:
        prev = f"${a['prev_low']:.0f}" if a.get("prev_low") else "—"
        rows += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0">🔻 <b>{a['origin']} → {a['destination']}</b></td>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0">{a['depart_date']} / Return {a.get('return_date','—')}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0">{a.get('airline','Unknown')}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#22c55e"><b>${a['price']:.0f}</b></td>
          <td style="padding:8px 12px;border-bottom:1px solid #f0f0f0;color:#94a3b8">{prev}</td>
        </tr>"""

    return f"""
    <html><body style="font-family:system-ui,sans-serif;color:#1e293b;max-width:700px;margin:0 auto">
      <div style="background:#1e3a5f;color:white;padding:20px 24px;border-radius:8px 8px 0 0">
        <h2 style="margin:0;font-size:18px">✈️ Travel Tracker — Flight Price Alert</h2>
        <p style="margin:4px 0 0;opacity:.7;font-size:13px">{len(alerts)} flight deal(s) found</p>
      </div>
      <div style="border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px;overflow:hidden">
        <table style="width:100%;border-collapse:collapse;font-size:14px">
          <thead>
            <tr style="background:#f8fafc">
              <th style="padding:10px 12px;text-align:left;font-weight:600">Route</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600">Dates</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600">Airline</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600">Price</th>
              <th style="padding:10px 12px;text-align:left;font-weight:600">Prev Low</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <p style="font-size:11px;color:#94a3b8;margin-top:12px">Six50 Travel Tracker · Auto-generated</p>
    </body></html>"""
