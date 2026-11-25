from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Aletheia"
admin.site.site_title = "Aletheia"
admin.site.index_title = "Administration"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("tool.urls")),
]
