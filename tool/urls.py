from django.urls import path, re_path

from . import canvas_views, views

urlpatterns = [
    path("", views.home, name="home"),
    path("demo/<slug:slug>/file/", views.demo_file, name="demo_file"),
    path("demo/<slug:slug>/start/", views.demo_start, name="demo_start"),
    path("demo/review/<int:attempt_id>/", views.demo_review, name="demo_review"),
    path("contact/", views.contact, name="contact"),
    path("terms/", views.terms, name="terms"),
    path("privacy/", views.privacy, name="privacy"),
    path("meet/slots/", views.meet_slots, name="meet_slots"),
    path("meet/confirm/<str:token>/", views.meet_confirm, name="meet_confirm"),
    path("meet/cancel/<str:token>/", views.meet_cancel, name="meet_cancel"),
    path("favicon.ico", views.icon),
    path("lti/icon.png", views.icon, name="lti_icon"),

    # LTI 1.3
    path("lti/login/", views.lti_login, name="lti_login"),
    path("lti/launch/", views.lti_launch, name="lti_launch"),
    path("lti/jwks/", views.jwks, name="lti_jwks"),
    path("lti/deep-link/", views.deep_link, name="deep_link"),
    re_path(r"^lti/register/canvas/?$", views.register_canvas, name="lti_register_canvas"),
    re_path(r"^lti/register/moodle/?$", views.register_canvas, name="lti_register_moodle"),
    path("lti/file/<str:token>/", views.lms_file, name="lms_file"),

    # Canvas through an API token (pilots without LTI)
    path("canvas/a/<int:assignment_id>/", canvas_views.frame, name="canvas_frame"),
    path("canvas/a/<int:assignment_id>/search/", canvas_views.search, name="canvas_search"),
    path("canvas/a/<int:assignment_id>/send/", canvas_views.send, name="canvas_send"),
    path("canvas/in/<str:token>/", canvas_views.sign_in_link, name="canvas_sign_in"),

    # Assignment
    path("a/<int:assignment_id>/", views.assignment_view, name="assignment"),
    path("a/<int:assignment_id>/settings/", views.settings_view, name="settings"),
    path("a/<int:assignment_id>/preview/", views.preview_start, name="preview_start"),
    path("a/<int:assignment_id>/preview/stop/", views.preview_stop, name="preview_stop"),
    path("a/<int:assignment_id>/submit/", views.submit, name="submit"),
    path("a/<int:assignment_id>/attempt/<int:attempt_id>/", views.attempt_view, name="attempt"),
    path("a/<int:assignment_id>/attempt/<int:attempt_id>/status/", views.attempt_status, name="attempt_status"),
    path("a/<int:assignment_id>/attempt/<int:attempt_id>/begin/", views.begin, name="begin"),
    path("a/<int:assignment_id>/attempt/<int:attempt_id>/answer/", views.answer, name="answer"),
    path("a/<int:assignment_id>/attempt/<int:attempt_id>/event/", views.log_event, name="log_event"),
    path("a/<int:assignment_id>/attempt/<int:attempt_id>/room-check/", views.room_check, name="room_check"),
    path("a/<int:assignment_id>/attempt/<int:attempt_id>/feedback/", views.feedback, name="feedback"),
    path("a/<int:assignment_id>/attempt/<int:attempt_id>/confirm/", views.confirm, name="confirm"),
    path("a/<int:assignment_id>/attempt/<int:attempt_id>/start/", views.start_in_tab, name="start_in_tab"),
    path("a/<int:assignment_id>/attempt/<int:attempt_id>/discard/", views.discard, name="discard"),
    path("a/<int:assignment_id>/attempt/<int:attempt_id>/preview/", views.upload_preview, name="upload_preview"),
    path("a/<int:assignment_id>/room/", views.room_screen, name="room_screen"),
    path("a/<int:assignment_id>/room/word/", views.room_word, name="room_word"),

    # Instructor review
    path("a/<int:assignment_id>/review/<int:attempt_id>/", views.attempt_detail, name="attempt_detail"),
    path("a/<int:assignment_id>/review/<int:attempt_id>/file/", views.attempt_file, name="attempt_file"),
    path("a/<int:assignment_id>/review/<int:attempt_id>/invite/", views.invite, name="invite"),
    path("a/<int:assignment_id>/review/<int:attempt_id>/uninvite/", views.withdraw_invitation, name="withdraw_invitation"),
]

# Local development only (404s unless DEBUG is on).
urlpatterns += [
    path("dev/launch/<str:role>/", views.dev_launch, name="dev_launch"),
    path("dev/reset/", views.dev_reset, name="dev_reset"),
]
