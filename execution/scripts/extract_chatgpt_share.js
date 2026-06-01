const puppeteer = require('puppeteer');

async function extractConversation() {
  const browser = await puppeteer.launch({
    headless: true,
    args: ['--no-sandbox', '--disable-setuid-sandbox']
  });
  
  try {
    const page = await browser.newPage();
    
    // Intercept network requests to find API responses
    const apiResponses = [];
    page.on('response', async (response) => {
      const url = response.url();
      if (url.includes('api') || url.includes('conversation') || url.includes('share')) {
        try {
          const text = await response.text();
          if (text.length > 100 && (text.includes('email') || text.includes('Example Advisory') || text.includes('Mónica') || text.includes('INSS'))) {
            apiResponses.push({ url, text: text.substring(0, 5000) });
          }
        } catch (e) {
          // Ignore
        }
      }
    });
    
    await page.goto('https://chatgpt.com/share/69441eb0-0650-8012-aa5d-2e795fba8d6f', {
      waitUntil: 'networkidle0',
      timeout: 60000
    });
    
    // Wait much longer for React to fully render
    await new Promise(resolve => setTimeout(resolve, 15000));
    
    // Try multiple times to get content as it loads
    let pageContent = '';
    for (let i = 0; i < 3; i++) {
      await new Promise(resolve => setTimeout(resolve, 5000));
      const content = await page.evaluate(() => {
        // Try to find message content
        const possibleSelectors = [
          'div[class*="markdown"]',
          'div[class*="message"]',
          'div[class*="conversation"]',
          'main div',
          'article div',
          '[role="main"] div'
        ];
        
        for (const selector of possibleSelectors) {
          const elements = document.querySelectorAll(selector);
          if (elements.length > 0) {
            const text = Array.from(elements)
              .map(el => el.innerText)
              .filter(t => t.length > 50)
              .join('\n\n');
            if (text.length > 200) return text;
          }
        }
        
        // Fallback: get visible text
        const body = document.body;
        const allText = body.innerText || body.textContent || '';
        const lines = allText.split('\n')
          .map(l => l.trim())
          .filter(l => l.length > 10)
          .filter(l => !l.match(/^(Skip|Attach|Search|Cookie|Terms|Privacy|By messaging)/i))
          .filter(l => !l.match(/^window\.|^function|^import|^requestAnimationFrame/i));
        return lines.join('\n');
      });
      
      if (content.length > pageContent.length) {
        pageContent = content;
      }
    }
    
    console.log('=== PAGE CONTENT ===');
    console.log(pageContent.substring(0, 5000));
    
    if (apiResponses.length > 0) {
      console.log('\n=== API RESPONSES ===');
      apiResponses.forEach((resp, i) => {
        console.log(`\nResponse ${i + 1} (${resp.url}):`);
        console.log(resp.text);
      });
    }
    
  } catch (error) {
    console.error('Error:', error.message);
    console.error(error.stack);
  } finally {
    await browser.close();
  }
}

extractConversation();

