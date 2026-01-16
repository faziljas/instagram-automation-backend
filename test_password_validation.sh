#!/bin/bash

echo "========================================="
echo "TESTING PASSWORD VALIDATION"
echo "========================================="
echo ""

echo "Step 1: Register and Login"
echo "---------------------------"
curl -X DELETE "http://localhost:8000/auth/cleanup/testpass@example.com" -s > /dev/null 2>&1
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "testpass@example.com", "password": "MyPass123"}' \
  -s > /dev/null

TOKEN=$(curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "testpass@example.com", "password": "MyPass123"}' \
  -s | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('access_token', ''))" 2>/dev/null)

echo "✅ Logged in"
echo ""

echo "Test 1: Try to change password to EXACT SAME password"
echo "-------------------------------------------------------"
curl -X PUT http://localhost:8000/users/me/password \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"old_password": "MyPass123", "new_password": "MyPass123"}' \
  -s | python3 -m json.tool
echo ""

echo "Test 2: Try to change password with DIFFERENT CASE"
echo "----------------------------------------------------"
curl -X PUT http://localhost:8000/users/me/password \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"old_password": "MyPass123", "new_password": "mypass123"}' \
  -s | python3 -m json.tool
echo ""

echo "Test 3: Try to change password with MIXED CASE variation"
echo "----------------------------------------------------------"
curl -X PUT http://localhost:8000/users/me/password \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"old_password": "MyPass123", "new_password": "MYPASS123"}' \
  -s | python3 -m json.tool
echo ""

echo "Test 4: Change to DIFFERENT password (should work)"
echo "----------------------------------------------------"
curl -X PUT http://localhost:8000/users/me/password \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"old_password": "MyPass123", "new_password": "NewPass456"}' \
  -s | python3 -m json.tool
echo ""

echo "Test 5: Verify new password works"
echo "----------------------------------"
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "testpass@example.com", "password": "NewPass456"}' \
  -s | python3 -m json.tool | head -5

echo ""
echo "========================================="
echo "✅ ALL VALIDATION TESTS COMPLETE!"
echo "========================================="
