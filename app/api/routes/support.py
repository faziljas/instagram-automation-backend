"""
Support Routes
Handles support-related endpoints like issue reporting
"""
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from app.dependencies.auth import get_current_user_id
from app.db.session import get_db
from app.models.user import User
from sqlalchemy.orm import Session

router = APIRouter()

# Email configuration from environment variables
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")  # Your email address to receive issue reports


class ReportIssueRequest(BaseModel):
    description: str
    body: str
    user_email: EmailStr
    user_name: str


@router.post("/report-issue")
def report_issue(
    request: ReportIssueRequest,
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """
    Send an issue report via email to the admin.
    """
    if not ADMIN_EMAIL:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Email service not configured. Please contact support directly."
        )

    if not SMTP_USERNAME or not SMTP_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Email service not configured. Please contact support directly."
        )

    # Verify the user making the request matches the email in the request
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    if user.email.lower() != request.user_email.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email mismatch"
        )

    try:
        # Create email message
        msg = MIMEMultipart()
        msg['From'] = SMTP_USERNAME
        msg['To'] = ADMIN_EMAIL
        msg['Subject'] = f"[LogicDM Support] Issue Report: {request.description[:50]}"
        
        # Email body
        email_body = f"""
New Issue Report from LogicDM

User Information:
- Name: {request.user_name}
- Email: {request.user_email}
- User ID: {user_id}

Issue Description:
{request.description}

Details:
{request.body}

---
This is an automated message from LogicDM Support System.
        """
        
        msg.attach(MIMEText(email_body, 'plain'))

        # Send email
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)

        print(f"✅ Issue report sent from {request.user_email} to {ADMIN_EMAIL}")

        return {
            "message": "Issue report sent successfully. We'll get back to you soon.",
            "status": "success"
        }

    except smtplib.SMTPException as e:
        print(f"❌ SMTP error sending issue report: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send email: {str(e)}"
        )
    except Exception as e:
        print(f"❌ Error sending issue report: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to send issue report: {str(e)}"
        )
