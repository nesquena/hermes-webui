#!/usr/bin/env python3
"""
Validation script for NIXON Workspace files
Checks HTML, CSS, and JS files for basic structure and syntax
"""

import os
import re
from pathlib import Path

def validate_html_file(filepath):
    """Validate HTML file structure"""
    print(f"🔍 Validating HTML: {filepath}")
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        checks = [
            ('DOCTYPE declaration', r'<!DOCTYPE html>'),
            ('HTML tag', r'<html[^>]*>'),
            ('Head section', r'<head>.*</head>'),
            ('Body section', r'<body>.*</body>'),
            ('CSS link', r'<link[^>]*nixon-workspace\.css'),
            ('JS script', r'<script[^>]*nixon-workspace\.js'),
            ('Main container', r'class="app-container"'),
            ('Header', r'class="app-header"'),
            ('Left sidebar', r'class="left-sidebar"'),
            ('Office workspace', r'class="office-workspace"'),
            ('Right sidebar', r'class="right-sidebar"'),
            ('Bottom panels', r'class="bottom-panels"'),
        ]
        
        passed = 0
        total = len(checks)
        
        for check_name, pattern in checks:
            if re.search(pattern, content, re.DOTALL | re.IGNORECASE):
                print(f"  ✅ {check_name}")
                passed += 1
            else:
                print(f"  ❌ {check_name}")
        
        print(f"  📊 HTML Validation: {passed}/{total} checks passed")
        return passed == total
        
    except Exception as e:
        print(f"  ❌ Error reading HTML file: {e}")
        return False

def validate_css_file(filepath):
    """Validate CSS file structure"""
    print(f"🎨 Validating CSS: {filepath}")
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        checks = [
            ('Root variables', r':root\s*{'),
            ('Font family', r'--font-family'),
            ('Color variables', r'--color-primary'),
            ('App container styles', r'\.app-container'),
            ('Header styles', r'\.app-header'),
            ('Sidebar styles', r'\.left-sidebar'),
            ('Office room styles', r'\.office-room'),
            ('Agent avatar styles', r'\.agent-avatar'),
            ('Responsive design', r'@media.*max-width'),
            ('Animations', r'@keyframes'),
        ]
        
        passed = 0
        total = len(checks)
        
        for check_name, pattern in checks:
            if re.search(pattern, content, re.DOTALL | re.IGNORECASE):
                print(f"  ✅ {check_name}")
                passed += 1
            else:
                print(f"  ❌ {check_name}")
        
        print(f"  📊 CSS Validation: {passed}/{total} checks passed")
        return passed == total
        
    except Exception as e:
        print(f"  ❌ Error reading CSS file: {e}")
        return False

def validate_js_file(filepath):
    """Validate JavaScript file structure"""
    print(f"⚡ Validating JavaScript: {filepath}")
    
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        checks = [
            ('NixonWorkspace class', r'class NixonWorkspace'),
            ('Constructor', r'constructor\(\)'),
            ('Agents array', r'this\.agents\s*='),
            ('Tasks array', r'this\.tasks\s*='),
            ('Event listeners', r'addEventListener'),
            ('Agent simulation', r'startAgentSimulation'),
            ('Update methods', r'updateAgentStatus'),
            ('Panel switching', r'setupPanelSwitching'),
            ('DOM ready handler', r'DOMContentLoaded'),
            ('Error handling', r'try.*catch'),
        ]
        
        passed = 0
        total = len(checks)
        
        for check_name, pattern in checks:
            if re.search(pattern, content, re.DOTALL | re.IGNORECASE):
                print(f"  ✅ {check_name}")
                passed += 1
            else:
                print(f"  ❌ {check_name}")
        
        print(f"  📊 JavaScript Validation: {passed}/{total} checks passed")
        return passed == total
        
    except Exception as e:
        print(f"  ❌ Error reading JavaScript file: {e}")
        return False

def validate_file_sizes():
    """Check file sizes are reasonable"""
    print(f"📏 Checking file sizes:")
    
    files = [
        ('nixon-workspace.html', 20000),  # ~20KB max
        ('nixon-workspace.css', 50000),   # ~50KB max
        ('nixon-workspace.js', 30000),    # ~30KB max
    ]
    
    all_good = True
    
    for filename, max_size in files:
        filepath = Path('static') / filename
        if filepath.exists():
            size = filepath.stat().st_size
            size_kb = size / 1024
            
            if size < max_size:
                print(f"  ✅ {filename}: {size_kb:.1f} KB (good size)")
            else:
                print(f"  ⚠️  {filename}: {size_kb:.1f} KB (might be too large)")
                all_good = False
        else:
            print(f"  ❌ {filename}: File not found")
            all_good = False
    
    return all_good

def main():
    print("🧪 NIXON Workspace - File Validation")
    print("=" * 50)
    
    base_path = Path('static')
    
    # Validate individual files
    html_valid = validate_html_file(base_path / 'nixon-workspace.html')
    css_valid = validate_css_file(base_path / 'nixon-workspace.css')
    js_valid = validate_js_file(base_path / 'nixon-workspace.js')
    sizes_ok = validate_file_sizes()
    
    print("\n" + "=" * 50)
    print("📊 VALIDATION SUMMARY")
    print("=" * 50)
    
    results = [
        ('HTML Structure', html_valid),
        ('CSS Styling', css_valid),
        ('JavaScript Logic', js_valid),
        ('File Sizes', sizes_ok)
    ]
    
    passed = sum(1 for _, valid in results if valid)
    total = len(results)
    
    for name, valid in results:
        status = "✅ PASS" if valid else "❌ FAIL"
        print(f"{name:20} {status}")
    
    print(f"\nOverall: {passed}/{total} validations passed")
    
    if passed == total:
        print("\n🎉 All validations passed! The workspace is ready for deployment.")
        return True
    else:
        print("\n⚠️  Some validations failed. Please review the issues above.")
        return False

if __name__ == "__main__":
    main()