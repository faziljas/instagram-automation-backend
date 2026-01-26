"""
Support Routes
Handles support-related endpoints like issue reporting
Uses Resend API (same as Supabase SMTP configuration) for sending emails
"""
import os
import requests
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr
from app.dependencies.auth import get_current_user_id
from app.db.session import get_db
from app.models.user import User
from sqlalchemy.orm import Session

router = APIRouter()

# Resend API configuration (same as Supabase SMTP setup)
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")  # Your email address to receive issue reports
SENDER_EMAIL = os.getenv("SENDER_EMAIL", "noreply@logicdm.app")  # From email (should match Supabase config)
SENDER_NAME = os.getenv("SENDER_NAME", "Logic DM")  # Sender name (should match Supabase config)


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
    Send an issue report via email to the admin using Resend API.
    Uses the same Resend service configured in Supabase.
    """
    if not ADMIN_EMAIL:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Email service not configured. Please contact support directly."
        )

    if not RESEND_API_KEY:
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
        # Prepare email content
        subject = f"[LogicDM Support] Issue Report: {request.description[:50]}"
        
        email_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
                .header {{ background-color: #4F46E5; color: white; padding: 20px; border-radius: 8px 8px 0 0; }}
                .content {{ background-color: #f9fafb; padding: 20px; border-radius: 0 0 8px 8px; }}
                .info-box {{ background-color: white; padding: 15px; margin: 15px 0; border-left: 4px solid #4F46E5; }}
                .issue-box {{ background-color: white; padding: 15px; margin: 15px 0; border-radius: 4px; }}
                h1 {{ margin: 0; }}
                h2 {{ color: #4F46E5; margin-top: 0; }}
                .label {{ font-weight: bold; color: #6B7280; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h1>New Issue Report from LogicDM</h1>
                </div>
                <div class="content">
                    <div class="info-box">
                        <h2>User Information</h2>
                        <p><span class="label">Name:</span> {request.user_name}</p>
                        <p><span class="label">Email:</span> {request.user_email}</p>
                        <p><span class="label">User ID:</span> {user_id}</p>
                    </div>
                    
                    <div class="issue-box">
                        <h2>Issue Description</h2>
                        <p>{request.description}</p>
                    </div>
                    
                    <div class="issue-box">
                        <h2>Details</h2>
                        <p style="white-space: pre-wrap;">{request.body}</p>
                    </div>
                    
                    <p style="color: #6B7280; font-size: 12px; margin-top: 20px;">
                        This is an automated message from LogicDM Support System.
                    </p>
                </div>
            </div>
        </body>
        </html>
        """
        
        email_text = f"""
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

        # Send email via Resend API
        resend_url = "https://api.resend.com/emails"
        headers = {
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "from": f"{SENDER_NAME} <{SENDER_EMAIL}>",
            "to": [ADMIN_EMAIL],
            "subject": subject,
            "html": email_html,
            "text": email_text
        }
        
        response = requests.post(resend_url, json=payload, headers=headers)
        
        if response.status_code == 200:
            print(f"✅ Issue report sent from {request.user_email} to {ADMIN_EMAIL} via Resend")
            return {
                "message": "Issue report sent successfully. We'll get back to you soon.",
                "status": "success"
            }
        else:
            error_msg = response.json().get("message", "Unknown error")
            print(f"❌ Resend API error: {response.status_code} - {error_msg}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to send email: {error_msg}"
            )

    except requests.exceptions.RequestException as e:
        print(f"❌ Request error sending issue report: {str(e)}")
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
