#!/bin/bash

echo "========================================="
echo "TESTING SETTINGS ENDPOINTS"
echo "========================================="
echo ""

echo "Step 1: Cleanup and register"
echo "----------------------------"
curl -X DELETE "http://localhost:8000/auth/cleanup/test@example.com" -s > /dev/null 2>&1
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "password123"}' \
  -s | python3 -m json.tool
echo ""

echo "Step 2: Login"
echo "-------------"
TOKEN=$(curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "password123"}' \
  -s | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('access_token', ''))" 2>/dev/null)

echo "Token: ${TOKEN:0:50}..."
echo ""

echo "Step 3: Update profile"
echo "----------------------"
curl -X PUT http://localhost:8000/users/me \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"first_name": "John", "last_name": "Doe"}' \
  -s | python3 -m json.tool
echo ""

echo "Step 4: Test wrong password"
echo "---------------------------"
curl -X PUT http://localhost:8000/users/me/password \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"old_password": "wrongpass", "new_password": "newpass123"}' \
  -s | python3 -m json.tool
echo ""

echo "Step 5: Change password correctly"
echo "----------------------------------"
curl -X PUT http://localhost:8000/users/me/password \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"old_password": "password123", "new_password": "newpass456"}' \
  -s | python3 -m json.tool
echo ""

echo "Step 6: Login with old password (should fail)"
echo "----------------------------------------------"
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "password123"}' \
  -s | python3 -m json.tool
echo ""

echo "Step 7: Login with new password (should work)"
echo "----------------------------------------------"
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "test@example.com", "password": "newpass456"}' \
  -s | python3 -m json.tool | head -5

echo ""
echo "========================================="
echo "✅ ALL TESTS COMPLETE!"
echo "========================================="
