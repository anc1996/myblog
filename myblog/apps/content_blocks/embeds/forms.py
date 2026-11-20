"""
Draftail「插入内嵌元素」自定义表单：同步采集 `媒体标题 / 无障碍描述` 与 `流媒体链接`。
"""

from django import forms

from .providers import resolve_embed_spec


class ControlledEmbedForm(forms.Form):
    title = forms.CharField(
        required=True,
        max_length=255,
        label="媒体标题 / 无障碍描述",
        help_text="请输入此视频或音频的真实名称。",
        widget=forms.TextInput(attrs={"placeholder": "例如：PostgreSQL 18 索引调优实战演示"}),
    )
    url = forms.URLField(
        required=True,
        label="流媒体链接",
        help_text="支持 B站、YouTube、Vimeo、腾讯视频、优酷、网易云音乐、Spotify、酷狗、咪咕、CodePen 等单页链接",
        widget=forms.URLInput(attrs={"placeholder": "https://www.bilibili.com/video/BV..."}),
    )

    def clean_url(self):
        raw_url = (self.cleaned_data.get("url") or "").strip()
        spec, err = resolve_embed_spec(raw_url)
        if not spec:
            raise forms.ValidationError(err)
        self.cleaned_data["_embed_spec"] = spec
        return spec.url
