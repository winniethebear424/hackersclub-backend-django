import os
import random
from datetime import timedelta

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.generics import CreateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.tokens import RefreshToken

from .models import CustomUser
from .serializers import (
    RegisterSerializer,
    ResendOTPSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)

User = get_user_model()


def send_email_ses(username, otp, email):
    ses = boto3.client("ses", region_name="us-west-1")
    try:
        response = ses.send_email(
            Destination={
                "ToAddresses": [email],
            },
            Message={
                "Body": {
                    "Html": {
                        "Charset": "UTF-8",
                        "Data": f"""
                            <p>Hello {username},</p>
                            <p>You requested a one-time password. Use this password to continue your process.</p>
                            <table width='100%'><tr><td style='text-align: center; font-size: 28px; font-weight: bold;'>{otp}</td></tr></table>
                            <p>If you didn't request this email, please ignore it.</p>
                            <p>-- Northeastern University Silicon Valley HackersClub</p>
                        """,
                    },
                },
                "Subject": {
                    "Charset": "UTF-8",
                    "Data": "Your one-time password",
                },
            },
            Source="vidyalathanataraja.r@northeastern.edu",
        )
    except (BotoCoreError, ClientError) as error:
        return {"success": False, "message": str(error)}
    else:
        return {"success": True, "message": response["MessageId"]}


class IsOwnerOrReadOnly(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        if request.method in permissions.SAFE_METHODS:
            return True
        return obj == request.user


class IsOwnerOrAdmin(permissions.BasePermission):
    def has_object_permission(self, request, view, obj):
        return obj == request.user or request.user.role == CustomUser.ADMIN


class UserViewSet(viewsets.ModelViewSet):
    queryset = CustomUser.objects.all()
    serializer_class = UserSerializer
    # lookup_field = 'email'
    permission_classes = [IsOwnerOrReadOnly, IsAuthenticated]
    authentication_classes = [JWTAuthentication]
    http_method_names = ["get", "patch", "head", "options", "delete"]

    def get_permissions(self):
        if getattr(self, "swagger_fake_view", False):
            # VIEW USED FOR SCHEMA GENERATION PURPOSES
            return []
        if self.action == "create":
            # Allow any user (authenticated or not) to access this action
            raise PermissionDenied("This action is not allowed.")
        if self.action == "destroy":
            self.permission_classes = [IsOwnerOrAdmin]
        return super(UserViewSet, self).get_permissions()

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)

        if "profile_picture" in request.FILES:
            self.handle_profile_picture_upload(request, instance)
        return Response(serializer.data)

    def handle_profile_picture_upload(self, request, instance):
        file = request.FILES["profile_picture"]

        # Determine the S3 file name
        _, file_extension = os.path.splitext(file.name)
        s3_file_name = f"users/{instance.id}/profile_picture{file_extension}"

        # Save the file to S3
        default_storage.save(s3_file_name, file)

        # Update the URL of the profile picture
        instance.profile_picture = default_storage.url(s3_file_name)

        instance.save()


class RegisterView(CreateAPIView):
    queryset = User.objects.all()
    permission_classes = [permissions.AllowAny]
    serializer_class = RegisterSerializer

    def create(self, request, *args, **kwargs):
        response = super().create(request, *args, **kwargs)
        try:
            user = User.objects.get(email=request.data["email"])
        except User.DoesNotExist:
            return Response(
                {"error": "User could not be created."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        refresh = RefreshToken.for_user(user)
        email_response = send_email_ses(user.username, user.otp, user.email)

        if not email_response["success"]:
            return Response(
                {
                    "refresh": str(refresh),
                    "access": str(refresh.access_token),
                    **response.data,
                    "email_error": email_response["message"],
                },
                status=status.HTTP_200_OK,
            )

        return Response(
            {
                "refresh": str(refresh),
                "access": str(refresh.access_token),
                **response.data,
            },
            status=status.HTTP_200_OK,
        )


class VerifyEmail(APIView):
    serializer_class = VerifyEmailSerializer

    def post(self, request, *args, **kwargs):
        otp = request.data.get("otp")
        email = request.data.get("email")
        try:
            user = CustomUser.objects.get(email=email)
        except CustomUser.DoesNotExist:
            return Response(
                {"status": "User with email: {email} not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Check if OTP has expired
        if timezone.now() > user.otp_expiration:
            return Response(
                {"status": "OTP has expired"}, status=status.HTTP_400_BAD_REQUEST
            )

        # Check if OTP verification is blocked due to too many attempts
        if (
            user.otp_attempts >= 3
            and (timezone.now() - user.otp_attempts_timestamp).total_seconds() < 600
        ):
            return Response(
                {"status": "Too many failed attempts. Please try again later."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.is_verified:
            return Response(
                {"status": "Email already verified, please Login"},
                status=status.HTTP_200_OK,
            )
        elif otp == user.otp:
            user.is_verified = True
            user.otp_attempts = 0
            user.otp_attempts_timestamp = None
            user.save()
            return Response(
                {"status": "Email verified, please proceed to Login page"},
                status=status.HTTP_200_OK,
            )
        else:
            user.otp_attempts += 1
            if user.otp_attempts == 1:
                user.otp_attempts_timestamp = timezone.now()
            user.save()
            return Response(
                {"status": "Invalid OTP"}, status=status.HTTP_400_BAD_REQUEST
            )


class ResendOTP(APIView):
    serializer_class = ResendOTPSerializer

    def post(self, request, *args, **kwargs):
        email = request.data.get("email")
        try:
            user = CustomUser.objects.get(email=email)
        except CustomUser.DoesNotExist:
            return Response(
                {"status": "User with email: {email} not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if user.is_verified:
            return Response(
                {"status": "Email already verified, please Login"},
                status=status.HTTP_200_OK,
            )
        user.otp = str(random.randint(100000, 999999))
        user.otp_created_at = timezone.now()
        user.otp_expiration = timezone.now() + timedelta(minutes=10)
        user.save()

        email_response = send_email_ses(user.username, user.otp, user.email)
        if not email_response["success"]:
            return Response(
                {"email_error": "OTP not sent", "message": email_response["message"]},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            {"status": "New OTP sent, please check your email."},
            status=status.HTTP_200_OK,
        )
