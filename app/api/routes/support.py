"""
Support Routes
Handles support-related endpoints like issue reporting
Uses Resend API (same as Supabase SMTP configuration) for sending emails
Supports file attachments (photos/videos)
"""
import os
import base64
import requests
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from typing import List, Optional
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

# File upload limits
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB
MAX_FILES = 5  # Maximum number of attachments


@router.post("/report-issue")
async def report_issue(
    description: str = Form(...),
    body: str = Form(...),
    user_email: str = Form(...),
    user_name: str = Form(...),
    attachments: Optional[List[UploadFile]] = File(None),
    db: Session = Depends(get_db),
    user_id: int = Depends(get_current_user_id)
):
    """
    Send an issue report via email to the admin using Resend API.
    Supports file attachments (photos/videos).
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

    if user.email.lower() != user_email.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email mismatch"
        )

    # Validate attachments
    attachment_list = attachments or []
    if len(attachment_list) > MAX_FILES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {MAX_FILES} attachments allowed"
        )

    attachment_data = []
    for attachment in attachment_list:
        # Check file size
        content = await attachment.read()
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File '{attachment.filename}' exceeds maximum size of 10MB"
            )
        
        # Check file type
        content_type = attachment.content_type or ""
        if not (content_type.startswith('image/') or content_type.startswith('video/')):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File '{attachment.filename}' is not a supported image or video format"
            )
        
        # Convert to base64 for email attachment
        base64_content = base64.b64encode(content).decode('utf-8')
        attachment_data.append({
            "filename": attachment.filename,
            "content": base64_content,
            "content_type": content_type
        })
        
        # Reset file pointer for potential reuse
        await attachment.seek(0)

    try:
        # Prepare email content
        subject = f"[LogicDM Support] Issue Report: {description[:50]}"
        
        # Build attachment info HTML
        attachment_html = ""
        if attachment_data:
            attachment_html = """
                    <div class="issue-box">
                        <h2>Attachments ({count})</h2>
                        <ul style="list-style: none; padding: 0;">
            """.format(count=len(attachment_data))
            for att in attachment_data:
                file_type = "📷 Image" if att["content_type"].startswith('image/') else "🎥 Video"
                attachment_html += f"""
                            <li style="padding: 5px 0;">
                                {file_type}: <strong>{att['filename']}</strong> ({att['content_type']})
                            </li>
                """
            attachment_html += """
                        </ul>
                    </div>
            """
        
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
                        <p><span class="label">Name:</span> {user_name}</p>
                        <p><span class="label">Email:</span> {user_email}</p>
                        <p><span class="label">User ID:</span> {user_id}</p>
                    </div>
                    
                    <div class="issue-box">
                        <h2>Issue Description</h2>
                        <p>{description}</p>
                    </div>
                    
                    <div class="issue-box">
                        <h2>Details</h2>
                        <p style="white-space: pre-wrap;">{body}</p>
                    </div>
                    
                    {attachment_html}
                    
                    <p style="color: #6B7280; font-size: 12px; margin-top: 20px;">
                        This is an automated message from LogicDM Support System.
                    </p>
                </div>
            </div>
        </body>
        </html>
        """
        
        attachment_text = ""
        if attachment_data:
            attachment_text = "\n\nAttachments:\n"
            for att in attachment_data:
                file_type = "Image" if att["content_type"].startswith('image/') else "Video"
                attachment_text += f"- {file_type}: {att['filename']} ({att['content_type']})\n"
        
        email_text = f"""
New Issue Report from LogicDM

User Information:
- Name: {user_name}
- Email: {user_email}
- User ID: {user_id}

Issue Description:
{description}

Details:
{body}
{attachment_text}
---
This is an automated message from LogicDM Support System.
        """

        # Prepare Resend API payload
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
        
        # Add attachments if any
        if attachment_data:
            payload["attachments"] = []
            for att in attachment_data:
                payload["attachments"].append({
                    "filename": att["filename"],
                    "content": att["content"]
                })
        
        response = requests.post(resend_url, json=payload, headers=headers)
        
        if response.status_code == 200:
            print(f"✅ Issue report sent from {user_email} to {ADMIN_EMAIL} via Resend")
            if attachment_data:
                print(f"   📎 Included {len(attachment_data)} attachment(s)")
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

    except HTTPException:
        raise
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
