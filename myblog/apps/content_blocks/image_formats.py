"""
全站 Wagtail RichText (Draftail) 统一图片排版格式注册表。

1. 覆盖 Wagtail 默认 fullwidth / left / right 格式并新增 centered（居中展示），
   保证所有历史 <embed embedtype="image" format="..."/> 数据无缝继承统一排版类；
2. 通过 ControlledFormat 注入 data-asset-id 与 data-full-src（/media-gateway/image/<uuid>/），
   使点击 RichText 缩略图时灯箱可直接弹出受控原图而非模糊派生图。
"""

from django.urls import reverse
from django.utils.html import escape
from wagtail.images.formats import (
    FORMATS_BY_NAME,
    Format,
    register_image_format,
    unregister_image_format,
)
from wagtail.images.shortcuts import get_rendition_or_not_found


class ControlledFormat(Format):
    """为 RichText 图片自动注入受控媒体网关原图标识与懒加载属性。"""

    def image_to_html(self, image, alt_text, extra_attributes=None):
        attrs = dict(extra_attributes) if extra_attributes else {}
        rendition = get_rendition_or_not_found(image, self.filter_spec)

        attrs["alt"] = escape(alt_text)
        attrs.setdefault("loading", "lazy")
        if self.classname:
            attrs["class"] = "%s" % escape(self.classname)

        public_id = getattr(image, "public_id", None)
        if public_id:
            pid_str = str(public_id)
            attrs["data-asset-id"] = pid_str
            try:
                attrs["data-full-src"] = reverse(
                    "media_gateway_asset",
                    kwargs={"asset_type": "image", "asset_id": public_id},
                )
            except Exception:
                pass

        return rendition.img_tag(attrs)


def _replace_format(fmt: Format) -> None:
    if fmt.name in FORMATS_BY_NAME:
        unregister_image_format(fmt.name)
    register_image_format(fmt)


_replace_format(
    ControlledFormat(
        "centered",
        "居中展示",
        "richtext-image centered cb-image-img cb-image--center",
        "width-800",
    )
)
_replace_format(
    ControlledFormat(
        "fullwidth",
        "全宽撑满",
        "richtext-image full-width cb-image-img cb-image--full",
        "width-1200",
    )
)
_replace_format(
    ControlledFormat(
        "left",
        "左图右文（环绕）",
        "richtext-image left cb-image-img cb-image--left",
        "width-500",
    )
)
_replace_format(
    ControlledFormat(
        "right",
        "右图左文（环绕）",
        "richtext-image right cb-image-img cb-image--right",
        "width-500",
    )
)
