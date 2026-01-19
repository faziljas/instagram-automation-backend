# Lead Capture Validation - Complete Explanation

## Overview
The lead capture system uses **strict validation** to ensure only valid email addresses and phone numbers are saved to your database. Invalid inputs are rejected with helpful error messages.

---

## Email Validation

### Validation Rules

1. **Format Check**: Must match standard email format: `username@domain.com`
2. **Required Components**:
   - Local part (before @): At least 1 character
   - @ symbol: Exactly one
   - Domain part: Minimum 4 characters (e.g., `x.co`)
   - TLD (extension): Minimum 2 characters (e.g., `.com`, `.org`)

3. **Common Mistakes Detected**:
   - Starting with `.` or `@`
   - Multiple `@` symbols
   - Consecutive dots (`..`)
   - Empty local or domain parts
   - Invalid TLDs

4. **Fake Email Rejection**:
   - Blocks common test patterns like: `test@test.com`, `abc@abc.com`, `123@123.com`

### ✅ Valid Email Examples
```
john.doe@example.com        ✅ Valid
user123@gmail.com          ✅ Valid
test_email@company.co.uk   ✅ Valid (international domain)
firstname.lastname@org.io  ✅ Valid
contact+tag@domain.net     ✅ Valid
```

### ❌ Invalid Email Examples

| Invalid Email | Error Message | Reason |
|--------------|---------------|---------|
| `invalid` | "Please enter a valid email address (e.g., example@domain.com)" | Missing @ and domain |
| `@example.com` | "Email cannot start with a dot or @ symbol." | Missing local part |
| `user@@domain.com` | "Email must contain exactly one @ symbol." | Multiple @ symbols |
| `user..name@domain.com` | "Email cannot contain consecutive dots." | Consecutive dots |
| `user@domain` | "Email domain must contain a dot (e.g., gmail.com)." | Missing TLD |
| `user@d.c` | "Email must have a valid domain extension (e.g., .com, .org)." | TLD too short |
| `test@test.com` | "Please enter a real email address." | Common fake pattern |
| `` | "Email cannot be empty." | Empty input |

---

## Phone Number Validation

### Validation Rules

1. **Format**: Accepts digits and common formatting characters:
   - Digits: `0-9`
   - Spaces, dashes, parentheses, dots, plus signs are allowed and removed

2. **Length**: 10-15 digits (international standard)

3. **Common Formats Accepted**:
   ```
   (555) 123-4567     → 5551234567
   +1-555-123-4567    → 15551234567
   555.123.4567       → 5551234567
   555 123 4567       → 5551234567
   +44 20 1234 5678   → 442012345678 (UK)
   ```

4. **Fake Number Detection**:
   - Blocks all same digits (1111111111, 0000000000)
   - Blocks sequential patterns (1234567890, 9876543210)
   - Blocks other common fake patterns

### ✅ Valid Phone Examples
```
(555) 123-4567        ✅ Valid (10 digits)
+1-555-123-4567       ✅ Valid (11 digits with country code)
555.123.4567          ✅ Valid (10 digits)
+44 20 1234 5678      ✅ Valid (UK format, 11 digits)
15551234567           ✅ Valid (11 digits)
```

### ❌ Invalid Phone Examples

| Invalid Phone | Error Message | Reason |
|--------------|---------------|---------|
| `123456789` | "Phone number is too short. Please include area code (minimum 10 digits)." | Too short (< 10 digits) |
| `1234567890123456789` | "Phone number is too long (maximum 15 digits)." | Too long (> 15 digits) |
| `abc1234567` | "Phone number can only contain digits, spaces, dashes, and parentheses." | Contains letters |
| `1111111111` | "This doesn't look like a valid phone number. Please check and try again." | All same digits |
| `1234567890` | "Please enter a real phone number." | Sequential pattern |
| `` | "Phone number cannot be empty." | Empty input |

---

## How Validation Works in Practice

### Flow Example: Email Collection

```
Step 1: Bot asks for email
├─ Bot: "What's your email address?"

Step 2: User responds
├─ User: "invalid-email"  ← Invalid!

Step 3: Validation Check
├─ System validates: validate_email("invalid-email")
├─ Result: ❌ False
├─ Error: "Please enter a valid email address (e.g., example@domain.com)"

Step 4: Bot responds with error
├─ Bot: "Please enter a valid email address (e.g., example@domain.com)
         What's your email address?"
├─ Flow returns to Step 1 (ask again)

Step 5: User tries again
├─ User: "john@example.com"  ← Valid!

Step 6: Validation Check
├─ System validates: validate_email("john@example.com")
├─ Result: ✅ True

Step 7: Save to Database
├─ CapturedLead {
│     email: "john@example.com",
│     captured_at: "2026-01-19 14:30:00"
│   }
├─ ✅ Lead saved!

Step 8: Send Reward
├─ Bot: "Thanks! Here's your reward: https://example.com/coupon"
```

---

## Code Implementation

### Validation Functions

```python
# Email Validation
def validate_email(email: str) -> Tuple[bool, str]:
    """
    Returns:
        (True, "") if valid
        (False, error_message) if invalid
    """
    # Checks:
    # 1. Not empty
    # 2. Format: user@domain.tld
    # 3. Only one @
    # 4. Valid domain structure
    # 5. Not a fake/test email
    
# Phone Validation
def validate_phone(phone: str) -> Tuple[bool, str]:
    """
    Returns:
        (True, "") if valid
        (False, error_message) if invalid
    """
    # Checks:
    # 1. Not empty
    # 2. Only digits (after removing formatting)
    # 3. Length: 10-15 digits
    # 4. Not all same digits
    # 5. Not sequential patterns
```

### Validation in Lead Capture Flow

```python
# In process_lead_capture_step():
if field_type == "email" or validation == "email":
    is_valid, error_message = validate_email(user_message.strip())
    
    if not is_valid:
        # Return error message + original question
        return {
            "action": "ask",
            "message": f"{error_message}\n\n{original_question}",
            "saved_lead": None,
            "validation_failed": True
        }
```

---

## Database Protection

### Invalid Data Prevention

1. **Validation Before Save**: Data is validated **before** being saved to the database
2. **No Invalid Records**: Invalid emails/phones never enter the `captured_leads` table
3. **User Feedback**: Users receive immediate feedback on what's wrong

### What Gets Saved

```python
# Only valid data is saved:
CapturedLead(
    email="john.doe@example.com",  # ✅ Validated
    # OR
    phone="15551234567",            # ✅ Validated
    captured_at=datetime.utcnow()
)
```

---

## Real-World Scenarios

### Scenario 1: User Makes Typo

```
Bot: "What's your email address?"
User: "john@example"  ← Missing TLD
Bot: "Please enter a valid email address (e.g., example@domain.com)
      What's your email address?"
User: "john@example.com"  ← Fixed!
Bot: "Thanks! Here's your reward: https://example.com/coupon"
✅ Lead saved: john@example.com
```

### Scenario 2: User Tests System

```
Bot: "What's your email address?"
User: "test@test.com"  ← Fake email
Bot: "Please enter a real email address.
      What's your email address?"
User: "myreal@email.com"  ← Real email
Bot: "Thanks! Here's your reward: https://example.com/coupon"
✅ Lead saved: myreal@email.com
```

### Scenario 3: User Enters Random Text

```
Bot: "What's your phone number?"
User: "hello123"  ← Contains letters
Bot: "Phone number can only contain digits, spaces, dashes, and parentheses.
      What's your phone number?"
User: "5551234567"  ← Valid
Bot: "Thanks! Here's your reward: https://example.com/coupon"
✅ Lead saved: 5551234567
```

### Scenario 4: User Enters Sequential Number

```
Bot: "What's your phone number?"
User: "1234567890"  ← Sequential pattern
Bot: "Please enter a real phone number.
      What's your phone number?"
User: "5551234567"  ← Real number
Bot: "Thanks! Here's your reward: https://example.com/coupon"
✅ Lead saved: 5551234567
```

---

## Testing Validation

### Test Cases for Email

| Test Case | Expected Result |
|-----------|----------------|
| `valid@email.com` | ✅ Valid |
| `user@domain.co.uk` | ✅ Valid |
| `invalid` | ❌ Invalid - Missing @ |
| `@domain.com` | ❌ Invalid - No local part |
| `user@@domain.com` | ❌ Invalid - Multiple @ |
| `user..name@domain.com` | ❌ Invalid - Consecutive dots |
| `test@test.com` | ❌ Invalid - Fake pattern |
| `` | ❌ Invalid - Empty |

### Test Cases for Phone

| Test Case | Expected Result |
|-----------|----------------|
| `(555) 123-4567` | ✅ Valid |
| `+1-555-123-4567` | ✅ Valid |
| `5551234567` | ✅ Valid |
| `123456789` | ❌ Invalid - Too short |
| `abc1234567` | ❌ Invalid - Contains letters |
| `1111111111` | ❌ Invalid - All same digits |
| `1234567890` | ❌ Invalid - Sequential |
| `` | ❌ Invalid - Empty |

---

## Summary

**Validation ensures:**
- ✅ Only valid, real email addresses and phone numbers are saved
- ✅ Users get helpful error messages when validation fails
- ✅ Database stays clean without fake/test data
- ✅ System handles edge cases and common mistakes gracefully

**What happens with invalid input:**
1. Validation fails
2. User receives specific error message
3. Original question is repeated
4. Flow continues until valid input is received
5. Only then is the lead saved to the database
