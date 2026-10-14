/* ================================================================
   个人主页 - 交互脚本
   功能：移动端菜单、导航高亮、滚动渐入动画、技能进度条、表单处理
   ================================================================ */

// 等 DOM 加载完成后执行（确保所有 HTML 元素都已存在）
document.addEventListener('DOMContentLoaded', function () {

  /* ============================================================
     1. 移动端汉堡菜单
     ============================================================ */
  var hamburger = document.getElementById('hamburger');
  var navMenu = document.getElementById('navMenu');

  // 点击汉堡按钮 → 切换菜单的打开/关闭状态
  hamburger.addEventListener('click', function () {
    hamburger.classList.toggle('active');  // 控制按钮动画（三横线变 X）
    navMenu.classList.toggle('active');     // 控制菜单的显示/隐藏
  });

  // 点击导航链接后自动关闭菜单（移动端体验优化）
  var navLinks = document.querySelectorAll('.nav-link');
  navLinks.forEach(function (link) {
    link.addEventListener('click', function () {
      hamburger.classList.remove('active');
      navMenu.classList.remove('active');
    });
  });

  /* ============================================================
     2. 导航栏滚动高亮
     当滚动到某个板块时，对应的导航链接自动高亮
     ============================================================ */
  var sections = document.querySelectorAll('section[id]');  // 所有带 id 的 section

  function highlightNavOnScroll() {
    var scrollY = window.pageYOffset;  // 当前滚动位置

    sections.forEach(function (section) {
      var sectionHeight = section.offsetHeight;        // 板块高度
      var sectionTop = section.offsetTop - 100;         // 板块顶部位置（减去导航栏高度）
      var sectionId = section.getAttribute('id');       // 板块 id

      // 判断当前滚动位置是否在该板块范围内
      if (scrollY > sectionTop && scrollY <= sectionTop + sectionHeight) {
        // 移除所有链接的 active 类
        navLinks.forEach(function (link) {
          link.classList.remove('active');
        });
        // 给对应链接添加 active 类
        var activeLink = document.querySelector('.nav-link[href="#' + sectionId + '"]');
        if (activeLink) {
          activeLink.classList.add('active');
        }
      }
    });
  }

  // 监听滚动事件（使用节流优化性能，避免频繁触发）
  var scrollTimer = null;
  window.addEventListener('scroll', function () {
    if (scrollTimer) clearTimeout(scrollTimer);
    scrollTimer = setTimeout(highlightNavOnScroll, 50);
  });

  /* ============================================================
     3. 滚动渐入动画
     使用 IntersectionObserver 检测元素是否进入视口
     进入后添加 .visible 类，触发 CSS 过渡动画
     ============================================================ */

  // 为各板块的内容元素添加 fade-in 类
  var fadeElements = document.querySelectorAll(
    '.skill-card, .project-card, .about-text, .about-avatar, .contact-item, .contact-form'
  );
  fadeElements.forEach(function (el) {
    el.classList.add('fade-in');
  });

  // 创建 IntersectionObserver 实例
  var observer = new IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          // 元素进入视口，添加 visible 类触发动画
          entry.target.classList.add('visible');
          // 动画只触发一次，之后不再监听
          observer.unobserve(entry.target);
        }
      });
    },
    {
      threshold: 0.15,  // 当元素 15% 进入视口时触发
    }
  );

  // 对所有需要渐入的元素进行监听
  fadeElements.forEach(function (el) {
    observer.observe(el);
  });

  /* ============================================================
     4. 技能进度条动画
     当技能卡片进入视口时，进度条从 0 增长到目标百分比
     ============================================================ */
  var skillCards = document.querySelectorAll('.skill-card');

  var skillObserver = new IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          // 获取该卡片的目标百分比（从 data-percent 属性读取）
          var percent = entry.target.getAttribute('data-percent');
          var fill = entry.target.querySelector('.skill-bar-fill');
          if (fill) {
            // 设置宽度，触发 CSS transition 动画
            fill.style.width = percent + '%';
          }
          observer.unobserve(entry.target);
        }
      });
    },
    {
      threshold: 0.3,
    }
  );

  skillCards.forEach(function (card) {
    skillObserver.observe(card);
  });

  /* ============================================================
     5. 联系表单提交处理
     前端模拟提交（实际项目中应替换为真实的 API 请求）
     ============================================================ */
  var contactForm = document.getElementById('contactForm');
  var formMessage = document.getElementById('formMessage');

  contactForm.addEventListener('submit', function (e) {
    e.preventDefault();  // 阻止表单默认提交行为（避免页面刷新）

    // 获取用户输入的值
    var name = document.getElementById('name').value.trim();
    var email = document.getElementById('email').value.trim();
    var message = document.getElementById('message').value.trim();

    // 简单的表单验证
    if (!name || !email || !message) {
      formMessage.style.color = '#e74c3c';
      formMessage.textContent = '请填写所有必填项！';
      return;
    }

    // 简单的邮箱格式验证
    var emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!emailRegex.test(email)) {
      formMessage.style.color = '#e74c3c';
      formMessage.textContent = '请输入有效的邮箱地址！';
      return;
    }

    // 模拟发送请求（显示加载状态）
    formMessage.style.color = '#636e72';
    formMessage.textContent = '正在发送...';

    // 模拟网络延迟后显示成功信息
    setTimeout(function () {
      formMessage.style.color = '#00b894';
      formMessage.textContent = '✅ 消息已发送！我会尽快回复您。';
      contactForm.reset();  // 清空表单
    }, 1000);
  });

  /* ============================================================
     6. 导航栏滚动时的阴影增强效果
     ============================================================ */
  var navbar = document.getElementById('navbar');

  window.addEventListener('scroll', function () {
    // 当向下滚动超过 50px 时，导航栏阴影加深
    if (window.pageYOffset > 50) {
      navbar.style.boxShadow = '0 4px 20px rgba(0, 0, 0, 0.12)';
    } else {
      navbar.style.boxShadow = '0 2px 8px rgba(0, 0, 0, 0.08)';
    }
  });

});
