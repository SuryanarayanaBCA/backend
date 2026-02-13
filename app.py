from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from flask import render_template_string
from xhtml2pdf import pisa

import firebase_admin
from firebase_admin import credentials, auth

import mysql.connector
import os
from xhtml2pdf import pisa
from datetime import datetime, timedelta
# PDF + QR
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
import qrcode

from dotenv import load_dotenv
import os

load_dotenv()

# Platypus (Modern PDF Design)
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    Image
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch

from datetime import datetime
import smtplib
from email.message import EmailMessage

import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException
import base64

# ---------------- APP SETUP ----------------
app = Flask(__name__)

# ✅ CORS: allow Netlify + local (set FRONTEND_ORIGINS in Koyeb)
frontend_origins = os.getenv(
    "FRONTEND_ORIGINS",
    "http://localhost:5500,http://127.0.0.1:5500"
)

origins_list = [o.strip() for o in frontend_origins.split(",") if o.strip()]

CORS(
    app,
    resources={r"/api/*": {"origins": origins_list}},
    supports_credentials=True,
    allow_headers=["Authorization", "Content-Type"],
    methods=["GET", "POST", "OPTIONS"]
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))



# ---------------- FIREBASE INIT ----------------
import json

if not firebase_admin._apps:
    firebase_json = os.getenv("FIREBASE_KEY_JSON")

    if not firebase_json:
        raise RuntimeError("FIREBASE_KEY_JSON env var missing in Koyeb")

    cred_dict = json.loads(firebase_json)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)

    
    
    # ---------------- BREVO CONFIG ----------------
BREVO_API_KEY = os.getenv("BREVO_API_KEY")
brevo_config = sib_api_v3_sdk.Configuration()
brevo_config.api_key['api-key'] = BREVO_API_KEY
brevo_api = sib_api_v3_sdk.TransactionalEmailsApi(
    sib_api_v3_sdk.ApiClient(brevo_config)
)


# ---------------- MYSQL CONFIG ----------------
db_config = {
    "host": os.getenv("DB_HOST"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
    "autocommit": True
}


def get_db():
    return mysql.connector.connect(**db_config)


# ---------------- TOKEN VERIFY ----------------
def verify_token():
    auth_header = request.headers.get("Authorization")

    if not auth_header:
        return None, ("Authorization header missing", 401)

    if not auth_header.startswith("Bearer "):
        return None, ("Invalid Authorization format", 401)

    token = auth_header.split("Bearer ")[1]

    try:
        decoded = auth.verify_id_token(token, clock_skew_seconds=10)
        return decoded, None
    except Exception as e:
        print("Firebase Token Error:", e)
        return None, ("Invalid Firebase token", 401)


# ---------------- GET USER EMAIL ----------------
def get_user_email(firebase_uid):
    user = auth.get_user(firebase_uid)
    return user.email


# ================= ADD THIS FUNCTION HERE =================

def generate_ticket_pdf_and_send_email(ticket_id):
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM bookings WHERE id=%s", (ticket_id,))
    booking = cursor.fetchone()
    cursor.close()
    db.close()

    if not booking:
        return

    map_link = f"https://www.google.com/maps?q={booking['latitude']},{booking['longitude']}"

    tickets_dir = os.path.join(BASE_DIR, "tickets")
    qr_dir = os.path.join(BASE_DIR, "qr_codes")
    os.makedirs(tickets_dir, exist_ok=True)
    os.makedirs(qr_dir, exist_ok=True)

    pdf_path = os.path.join(tickets_dir, f"ticket_{ticket_id}.pdf")
    qr_path = os.path.join(qr_dir, f"qr_{ticket_id}.png")

    qrcode.make(map_link).save(qr_path)

    c = canvas.Canvas(pdf_path, pagesize=A4)
    w, h = A4

    c.setFont("Helvetica-Bold", 20)
    c.drawString(200, h - 80, "Parking Ticket")

    c.setFont("Helvetica", 12)
    c.drawString(80, h - 140, f"Ticket ID: {booking['id']}")
    c.drawString(80, h - 170, f"Slot No: {booking['slot_no']}")
    c.drawString(80, h - 200, f"Vehicle No: {booking['vehicle_no']}")
    c.drawString(80, h - 230, f"Location: {booking['location']}")
    c.drawString(80, h - 260, f"Date: {booking['booking_date']}")
    c.drawString(80, h - 290, f"Map: {map_link}")

    c.drawImage(ImageReader(qr_path), 200, h - 480, 150, 150)
    c.showPage()
    c.save()

    try:
        user_email = get_user_email(booking["firebase_uid"])

        send_ticket_email(
            user_email,
            "Parking Booking Confirmation",
            f"""
Your parking booking is confirmed.<br><br>
<b>Ticket ID:</b> {booking['id']}<br>
<b>Slot:</b> {booking['slot_no']}<br>
<b>Location:</b> {booking['location']}
""",
            pdf_path
        )

    except Exception as e:
        print("Auto email error:", e)


# =========================================================
# BOOKED SLOTS
# =========================================================
@app.route("/api/confirm-booking", methods=["POST"])
def confirm_booking():

    decoded, error = verify_token()
    if error:
        return jsonify({"error": error[0]}), error[1]

    data = request.get_json()

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        INSERT INTO bookings
        (firebase_uid, slot_no, vehicle_no, location,
         latitude, longitude,
         booking_date, created_at, entry_time)
        VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())
    """, (
        decoded["uid"],
        data["slot"],
        data["vehicle"],
        data["location"],
        data["latitude"],
        data["longitude"],
        data["date"]
    ))

    db.commit()

    ticket_id = cursor.lastrowid
    cursor.close()
    db.close()

    # ✅ Generate PDF + Send Email
    generate_ticket_pdf_and_send_email(ticket_id)

    return jsonify({
        "success": True,
        "ticket_id": ticket_id,
        "download_url": f"http://127.0.0.1:5000/api/ticket-pdf/{ticket_id}"
    }), 201

# =========================================================
# BOOKED SLOTS
# =========================================================
@app.route("/api/booked-slots", methods=["GET"])
def booked_slots():
    date = request.args.get("date")
    location = request.args.get("location")

    if not date or not location:
        return jsonify({"error": "Date and location required"}), 400

    db = get_db()
    cursor = db.cursor()

    cursor.execute("""
        SELECT slot_no FROM bookings
        WHERE booking_date=%s AND location=%s AND exit_time IS NULL
    """, (date, location))

    slots = [row[0] for row in cursor.fetchall()]
    cursor.close()
    db.close()

    return jsonify({"slots": slots}), 200

# =========================================================
# HOURLY TICKET PDF (MAP QR + AUTO EMAIL)
# =========================================================
@app.route("/api/ticket-pdf/<int:ticket_id>", methods=["GET"])
def ticket_pdf(ticket_id):

    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM bookings WHERE id=%s", (ticket_id,))
    booking = cursor.fetchone()
    cursor.close()
    db.close()

    if not booking:
        return jsonify({"error": "Ticket not found"}), 404

    map_link = f"https://www.google.com/maps?q={booking['latitude']},{booking['longitude']}"

    tickets_dir = os.path.join(BASE_DIR, "tickets")
    qr_dir = os.path.join(BASE_DIR, "qr_codes")
    os.makedirs(tickets_dir, exist_ok=True)
    os.makedirs(qr_dir, exist_ok=True)

    pdf_path = os.path.join(tickets_dir, f"ticket_{ticket_id}.pdf")
    qr_path = os.path.join(qr_dir, f"qr_{ticket_id}.png")

    # Generate QR
    qrcode.make(map_link).save(qr_path)

    # Create PDF
    c = canvas.Canvas(pdf_path, pagesize=A4)
    w, h = A4

    c.setFont("Helvetica-Bold", 20)
    c.drawString(200, h - 80, "Parking Ticket")

    c.setFont("Helvetica", 12)
    c.drawString(80, h - 140, f"Ticket ID: {booking['id']}")
    c.drawString(80, h - 170, f"Slot No: {booking['slot_no']}")
    c.drawString(80, h - 200, f"Vehicle No: {booking['vehicle_no']}")
    c.drawString(80, h - 230, f"Location: {booking['location']}")
    c.drawString(80, h - 260, f"Date: {booking['booking_date']}")
    c.drawString(80, h - 290, f"Map: {map_link}")

    c.drawImage(ImageReader(qr_path), 200, h - 480, 150, 150)
    c.showPage()
    c.save()

    

    return send_file(pdf_path, as_attachment=True)


# =========================================================
# MONTHLY TICKET PDF (MAP QR)
# =========================================================
# =========================================================
# CONFIRM MONTHLY BOOKING
# =========================================================
@app.route("/api/confirm-monthly-booking", methods=["POST"])
def confirm_monthly_booking():

    decoded, error = verify_token()
    if error:
        return jsonify({"error": error[0]}), error[1]

    data = request.get_json()

    if not data:
        return jsonify({"error": "No data received"}), 400

    # Validate required fields
    required_fields = [
        "customer_name",
        "vehicle_no",
        "phone_no",
        "location",
        "package_months",
        "amount"
    ]

    for field in required_fields:
        if not data.get(field):
            return jsonify({"error": f"{field} is required"}), 400

    try:
        db = get_db()
        cursor = db.cursor()

        # ✅ Ensure user exists (Fix foreign key issue)
        cursor.execute(
            "SELECT firebase_uid FROM users WHERE firebase_uid = %s",
            (decoded["uid"],)
        )
        existing_user = cursor.fetchone()

        if not existing_user:
            user_email = get_user_email(decoded["uid"])

            cursor.execute("""
                INSERT INTO users (firebase_uid, email)
                VALUES (%s, %s)
            """, (decoded["uid"], user_email))

            db.commit()

        # ✅ Get user email (for monthly booking table)
        user_email = get_user_email(decoded["uid"])

        # ✅ Calculate start and end date
        start_date = datetime.now().date()
        end_date = start_date + timedelta(days=int(data.get("package_months")) * 30)

        # ✅ Insert monthly booking
        cursor.execute("""
            INSERT INTO monthly_bookings
            (firebase_uid, customer_name, email, phone_no, vehicle_no, location,
             latitude, longitude, package_months, amount,
             start_date, end_date, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
        """, (
            decoded["uid"],
            data.get("customer_name"),
            user_email,
            data.get("phone_no"),
            data.get("vehicle_no"),
            data.get("location"),
            data.get("latitude"),
            data.get("longitude"),
            data.get("package_months"),
            data.get("amount"),
            start_date,
            end_date
        ))

        db.commit()
        monthly_id = cursor.lastrowid

        cursor.close()
        db.close()

        # Generate PDF + Send Email
        generate_monthly_ticket_pdf_and_send_email(monthly_id)

        return jsonify({
            "success": True,
            "monthly_id": monthly_id,
            "amount": data.get("amount")
        }), 201

    except Exception as e:
        print("Monthly Booking Error:", e)
        return jsonify({"error": "Internal Server Error"}), 500

# =========================================================
# GENERATE MONTHLY TICKET PDF + SEND EMAIL (MODERN SINGLE PAGE)
# =========================================================
def generate_monthly_ticket_pdf_and_send_email(monthly_id):

    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM monthly_bookings WHERE id=%s", (monthly_id,))
    booking = cursor.fetchone()
    cursor.close()
    db.close()

    if not booking:
        return None

    map_link = f"https://www.google.com/maps?q={booking['latitude']},{booking['longitude']}"

    monthly_dir = os.path.join(BASE_DIR, "monthly_tickets")
    qr_dir = os.path.join(BASE_DIR, "monthly_qr_codes")
    os.makedirs(monthly_dir, exist_ok=True)
    os.makedirs(qr_dir, exist_ok=True)

    pdf_path = os.path.join(monthly_dir, f"monthly_ticket_{monthly_id}.pdf")
    qr_path = os.path.join(qr_dir, f"monthly_qr_{monthly_id}.png")

    qrcode.make(map_link).save(qr_path)

    # =========================
    # 📄 MODERN SINGLE PAGE PDF
    # =========================
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.lib.pagesizes import A4

    doc = SimpleDocTemplate(
        pdf_path,
        pagesize=A4,
        rightMargin=30,
        leftMargin=30,
        topMargin=30,
        bottomMargin=30
    )

    styles = getSampleStyleSheet()
    elements = []

    # ===== HEADER BAR =====
    header_data = [[
        Paragraph(
            "<font size=20 color='white'><b>ParkSmart Monthly Pass</b></font>",
            styles["Normal"]
        )
    ]]

    header_table = Table(header_data, colWidths=[6.7 * inch])
    header_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#0f172a")),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 18),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 18),
    ]))

    elements.append(header_table)
    elements.append(Spacer(1, 15))

    # ===== TICKET ID + STATUS =====
    status_color = "#16a34a" if booking.get("payment_status", "Paid") == "Paid" else "#dc2626"

    id_status_table = Table([[
        Paragraph(f"<b>Ticket ID:</b> #{booking['id']}", styles["Normal"]),
        Paragraph(f"<b>Status:</b> <font color='{status_color}'>PAID</font>", styles["Normal"])
    ]], colWidths=[3.3 * inch, 3.4 * inch])

    elements.append(id_status_table)
    elements.append(Spacer(1, 15))

    # ===== COMPACT DETAILS GRID =====
    data = [
        ["Customer", booking["customer_name"], "Vehicle", booking["vehicle_no"]],
        ["Phone", booking["phone_no"], "Location", booking["location"]],
        ["Duration", f"{booking['package_months']} Months", "Amount", f"₹ {booking['amount']}"],
        ["Start", str(booking["start_date"]), "End", str(booking["end_date"])],
    ]

    details_table = Table(data, colWidths=[1.3*inch, 2.0*inch, 1.3*inch, 2.1*inch])

    details_table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,-1), colors.whitesmoke),
        ("GRID", (0,0), (-1,-1), 0.4, colors.HexColor("#d1d5db")),
        ("FONTSIZE", (0,0), (-1,-1), 10),
        ("LEFTPADDING", (0,0), (-1,-1), 8),
        ("RIGHTPADDING", (0,0), (-1,-1), 8),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))

    elements.append(details_table)
    elements.append(Spacer(1, 20))

    # ===== QR SECTION =====
    elements.append(Paragraph(
        "<b>Scan to View Parking Location</b>",
        ParagraphStyle(name="CenterQR", alignment=1)
    ))

    elements.append(Spacer(1, 10))

    qr_image = Image(qr_path, width=2.5 * inch, height=2.5 * inch)
    qr_wrapper = Table([[qr_image]], colWidths=[6.7 * inch])
    qr_wrapper.setStyle(TableStyle([
        ("ALIGN", (0,0), (-1,-1), "CENTER")
    ]))

    elements.append(qr_wrapper)
    elements.append(Spacer(1, 10))

    elements.append(Paragraph(
        f'<a href="{map_link}">Open Location in Google Maps</a>',
        ParagraphStyle(name="MapLink", alignment=1, textColor=colors.blue, fontSize=9)
    ))

    elements.append(Spacer(1, 20))

    # ===== FOOTER =====
    elements.append(Paragraph(
        "Thank you for choosing ParkSmart • support@parksmart.com",
        ParagraphStyle(name="Footer", alignment=1, fontSize=8, textColor=colors.grey)
    ))

    # Build PDF
    doc.build(elements)

    # =========================
    # 📧 SEND EMAIL
    # =========================
    try:
        user_email = get_user_email(booking["firebase_uid"])

        send_ticket_email(
            user_email,
            "Monthly Parking Pass Confirmation",
            f"""
Your monthly parking pass is confirmed.<br><br>
<b>Ticket ID:</b> {booking['id']}<br>
<b>Location:</b> {booking['location']}<br>
<b>Package:</b> {booking['package_months']} Months<br>
<b>Amount Paid:</b> ₹{booking['amount']}<br><br>
Your Monthly Pass PDF is attached.
""",
            pdf_path
        )

    except Exception as e:
        print("Monthly email error:", e)

    return pdf_path


# =========================================================
# ADMIN APIs (UNCHANGED)
# =========================================================
@app.route("/api/admin/bookings", methods=["GET"])
def admin_get_bookings():
    decoded, error = verify_token()
    if error:
        return jsonify({"error": error[0]}), error[1]

    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM bookings ORDER BY id DESC")
    data = cursor.fetchall()
    cursor.close()
    db.close()
    return jsonify(data)


@app.route("/api/admin/monthly-bookings", methods=["GET"])
def admin_monthly():
    db = get_db()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM monthly_bookings ORDER BY created_at DESC")
    data = cursor.fetchall()
    cursor.close()
    db.close()
    return jsonify(data)

#-------------------------------------------------------#
@app.route("/api/admin/revoke-booking", methods=["POST"])
def admin_revoke_booking():
    decoded, error = verify_token()
    if error:
        return jsonify({"error": error[0]}), error[1]

    data = request.get_json()
    booking_id = data.get("booking_id")

    if not booking_id:
        return jsonify({"error": "Booking ID required"}), 400

    db = get_db()
    cursor = db.cursor(dictionary=True)

    # 🔹 Fetch active booking
    cursor.execute("""
        SELECT entry_time
        FROM bookings
        WHERE id = %s AND exit_time IS NULL
    """, (booking_id,))
    booking = cursor.fetchone()

    if not booking:
        cursor.close()
        db.close()
        return jsonify({"error": "Active booking not found"}), 404

    entry_time = booking["entry_time"]
    exit_time = datetime.now()

    # ⏱️ Duration calculation
    duration_seconds = (exit_time - entry_time).total_seconds()
    total_hours = int(duration_seconds // 3600)

    # round up
    if duration_seconds % 3600 != 0:
        total_hours += 1

    if total_hours == 0:
        total_hours = 1

    # 💰 Pricing (change later if needed)
    RATE_PER_HOUR = 50
    parking_amount = total_hours * RATE_PER_HOUR

    # 🔁 Update booking
    cursor.execute("""
        UPDATE bookings
        SET exit_time = %s,
            total_hours = %s,
            parking_amount = %s,
            status = 'revoked'
        WHERE id = %s
    """, (
        exit_time,
        total_hours,
        parking_amount,
        booking_id
    ))

    db.commit()
    cursor.close()
    db.close()

    return jsonify({
        "success": True,
        "booking_id": booking_id,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "total_hours": total_hours,
        "amount": parking_amount
    }), 200


def send_ticket_email(to_email, subject, body, attachment_path=None):
    try:
        sender = {
            "name": "ParksMart",
            "email": "dmnprksmrt@gmail.com"  # must be verified in Brevo
        }

        to = [{"email": to_email}]

        attachments = []

        if attachment_path:
            with open(attachment_path, "rb") as f:
                encoded_file = base64.b64encode(f.read()).decode()

            attachments.append({
                "content": encoded_file,
                "name": os.path.basename(attachment_path)
            })

        email = sib_api_v3_sdk.SendSmtpEmail(
            to=to,
            sender=sender,
            subject=subject,
            html_content=f"<html><body><p>{body}</p></body></html>",
            attachment=attachments
        )

        brevo_api.send_transac_email(email)
        print("✅ Email sent successfully via Brevo")

    except ApiException as e:
        print("❌ Brevo Error:", e)


# ---------------- RUN SERVER ----------------
if __name__ == "__main__":
    app.run()


