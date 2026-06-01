// JavaScript bookmarklet to extract Twilio credentials from Console
// Run this in the browser console on https://console.twilio.com

(function() {
    console.log("Extracting Twilio credentials...");
    
    // Try to find Account SID from various locations
    let accountSid = null;
    let authToken = null;
    
    // Method 1: Check if credentials are in localStorage/sessionStorage
    for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key && key.includes('account') && key.includes('sid')) {
            const value = localStorage.getItem(key);
            if (value && value.startsWith('AC')) {
                accountSid = value;
            }
        }
    }
    
    // Method 2: Look for Account SID in page text
    const pageText = document.body.innerText;
    const sidMatch = pageText.match(/AC[a-zA-Z0-9]{32}/);
    if (sidMatch) {
        accountSid = sidMatch[0];
    }
    
    // Method 3: Look in data attributes or React props
    const reactRoot = document.querySelector('#root');
    if (reactRoot && reactRoot._reactInternalInstance) {
        // Try to traverse React tree (may not work due to React version)
        console.log("React root found, but direct access may be limited");
    }
    
    // Method 4: Check for Account SID in URL or page metadata
    const metaTags = document.querySelectorAll('meta[name*="account"], meta[property*="account"]');
    metaTags.forEach(meta => {
        const content = meta.content || meta.getAttribute('content');
        if (content && content.startsWith('AC')) {
            accountSid = content;
        }
    });
    
    // Output results
    console.log("\n=== Twilio Credentials Extraction ===");
    if (accountSid) {
        console.log("Account SID:", accountSid);
        console.log("\nCopy this command:");
        console.log(`python scripts/debug_twilio_sms.py --account-sid ${accountSid} --auth-token YOUR_TOKEN`);
    } else {
        console.log("⚠️  Account SID not found automatically");
        console.log("\nManual steps:");
        console.log("1. Go to: Account → Account Info");
        console.log("2. Copy Account SID (starts with AC...)");
        console.log("3. Click 'Show' next to Auth Token and copy it");
    }
    
    console.log("\nNote: Auth Token must be retrieved manually from Account Info page");
    console.log("      (it's hidden for security reasons)");
    
    return {
        accountSid: accountSid,
        note: "Auth Token must be retrieved manually from Account Info page"
    };
})();







