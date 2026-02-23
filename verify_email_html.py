#!/usr/bin/env python3
"""
验证邮件是否以HTML格式发送公关稿正文
使用方法: python verify_email_html.py <邮件文件路径>.eml
"""

import email
import sys
import html
from email import policy
from email.parser import BytesParser


def find_html_part(msg):
    """查找HTML部分，使用简单的walk方法避免递归问题"""
    try:
        # 使用walk()方法遍历所有部分，这是最安全的方法
        for part in msg.walk():
            try:
                content_type = part.get_content_type()
                if content_type == "text/html":
                    return part
            except Exception:
                # 跳过无法获取content-type的部分
                continue
    except Exception:
        pass
    
    # 如果walk()失败，尝试直接检查
    try:
        if not msg.is_multipart() and msg.get_content_type() == "text/html":
            return msg
    except Exception:
        pass
    
    return None


def extract_pr_body_from_html(html_content):
    """从HTML中提取公关稿正文部分"""
    # 查找"公關稿正文："后面的内容
    if "公關稿正文：" in html_content or "公關稿正文:" in html_content:
        # 尝试找到包含公关稿正文的div
        import re
        # 查找 <div style='line-height:1.7;font-size:14px;'> 后面的内容
        pattern = r'<div[^>]*line-height:1\.7[^>]*>(.*?)</div>'
        match = re.search(pattern, html_content, re.DOTALL)
        if match:
            return match.group(1)
        # 如果没找到，查找"公關稿正文："后面的内容
        pattern = r'公關稿正文[：:]\s*</strong></p>\s*<div[^>]*>(.*?)</div>'
        match = re.search(pattern, html_content, re.DOTALL)
        if match:
            return match.group(1)
    return None


def verify_email_html(eml_path):
    """验证邮件文件是否包含HTML格式的公关稿正文"""
    try:
        with open(eml_path, 'rb') as f:
            msg = BytesParser(policy=policy.default).parse(f)
        
        print(f"📧 邮件主题: {msg.get('Subject', 'N/A')}")
        print(f"📅 发送时间: {msg.get('Date', 'N/A')}")
        print(f"👤 发件人: {msg.get('From', 'N/A')}")
        print(f"👥 收件人: {msg.get('To', 'N/A')}")
        print("-" * 60)
        
        # 查找HTML部分
        html_part = find_html_part(msg)
        
        if not html_part:
            print("❌ 未找到HTML部分！邮件可能只包含纯文本。")
            print("\n尝试使用备用方法查找...")
            # 备用方法：直接遍历所有部分
            html_content = None
            for part in msg.walk():
                try:
                    if part.get_content_type() == "text/html":
                        html_content = part.get_payload(decode=True)
                        if isinstance(html_content, bytes):
                            html_content = html_content.decode('utf-8', errors='ignore')
                        print("✅ 使用备用方法找到HTML部分")
                        break
                except Exception:
                    continue
            
            if not html_content:
                print("❌ 备用方法也未找到HTML部分")
                return False
        else:
            print("✅ 找到HTML部分")
            
            # 获取HTML内容
            try:
                html_content = html_part.get_payload(decode=True)
                if isinstance(html_content, bytes):
                    html_content = html_content.decode('utf-8', errors='ignore')
            except Exception as e:
                print(f"⚠️  获取HTML内容时出错: {e}")
                return False
        
        # 检查是否包含HTML标签
        if not html_content:
            print("❌ HTML内容为空")
            return False
            
        has_html_tags = '<div' in html_content or '<p>' in html_content or '<strong>' in html_content
        if not has_html_tags:
            print("⚠️  HTML部分存在，但似乎不包含HTML标签")
            print(f"   内容预览: {html_content[:200]}...")
            return False
        
        print("✅ HTML部分包含HTML标签")
        
        # 检查是否包含公关稿正文
        if "公關稿正文" in html_content:
            print("✅ 找到公关稿正文部分")
            
            # 提取公关稿正文
            pr_body = extract_pr_body_from_html(html_content)
            if pr_body:
                # 解码HTML实体
                pr_body_clean = html.unescape(pr_body)
                # 移除HTML标签（简单处理）
                import re
                pr_body_text = re.sub(r'<[^>]+>', '', pr_body_clean)
                pr_body_text = ' '.join(pr_body_text.split())[:100]  # 前100个字符
                print(f"📝 公关稿正文预览（前100字符）: {pr_body_text}...")
                
                # 检查是否包含加粗标签（说明*xxx*已转换为<b>xxx</b>）
                if '<b>' in pr_body or '<strong>' in pr_body:
                    print("✅ 公关稿正文包含加粗格式（<b>或<strong>标签）")
                else:
                    print("⚠️  公关稿正文未发现加粗格式，可能*xxx*未正确转换")
                
                # 检查是否包含其他HTML格式
                if '<p>' in pr_body or '<br>' in pr_body:
                    print("✅ 公关稿正文包含段落或换行格式")
            else:
                print("⚠️  无法提取公关稿正文内容")
        else:
            print("❌ 未找到公关稿正文部分")
            return False
        
        # 检查邮件结构
        print("\n📋 邮件结构:")
        if msg.is_multipart():
            print(f"   邮件类型: multipart (包含 {len(list(msg.walk()))} 个部分)")
            for i, part in enumerate(msg.walk()):
                content_type = part.get_content_type()
                if content_type == "text/html":
                    print(f"   ✅ 部分 {i+1}: {content_type} (HTML格式)")
                elif content_type == "text/plain":
                    print(f"   📄 部分 {i+1}: {content_type} (纯文本格式)")
                elif not content_type.startswith("text/"):
                    print(f"   📎 部分 {i+1}: {content_type}")
        
        print("\n✅ 验证完成：邮件确实以HTML格式发送了公关稿正文！")
        return True
        
    except FileNotFoundError:
        print(f"❌ 错误：找不到文件 {eml_path}")
        return False
    except Exception as e:
        print(f"❌ 错误：{e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使用方法: python verify_email_html.py <邮件文件路径>.eml")
        print("示例: python verify_email_html.py \"C:\\Users\\wangz\\Downloads\\新稿件_ 許建德新賽季馬到功成，奪澳洲巴瑟斯特 12 小時賽季軍.eml\"")
        sys.exit(1)
    
    eml_path = sys.argv[1]
    verify_email_html(eml_path)
