// Twilio Credentials Extractor Bookmarklet
// 
// INSTRUCTIONS:
// 1. Go to: https://console.twilio.com/us1/develop/account/settings/general
// 2. Make sure you're logged in
// 3. Click 'Show' next to Auth Token to reveal it
// 4. Open browser console (F12 or Cmd+Option+I)
// 5. Paste this entire script and press Enter
// 6. Copy the output and run the command shown

(function() {
    console.log("=".repeat(60));
    console.log("Twilio Credentials Extractor");
    console.log("=".repeat(60));
    
    let accountSid = null;
    let authToken = null;
    
    // Get page text
    const bodyText = document.body.innerText || document.body.textContent || '';
    const pageHTML = document.documentElement.innerHTML || '';
    
    // Find Account SID (AC followed by 32 alphanumeric)
    const sidPattern = /AC[a-zA-Z0-9]{32}/g;
    const sidMatches = bodyText.match(sidPattern) || pageHTML.match(sidPattern) || [];
    if (sidMatches.length > 0) {
        // Get unique matches
        const uniqueSids = [...new Set(sidMatches)];
        accountSid = uniqueSids[0];
        console.log("✓ Found Account SID:", accountSid);
    }
    
    // Try to find Auth Token
    // Look for long alphanumeric strings that aren't Account SID
    const tokenPattern = /[a-zA-Z0-9]{32,}/g;
    const allTokens = (bodyText.match(tokenPattern) || []).concat(pageHTML.match(tokenPattern) || []);
    const uniqueTokens = [...new Set(allTokens)];
    
    // Filter out Account SID and other common patterns
    for (const token of uniqueTokens) {
        if (token.length >= 32 && 
            !token.startsWith('AC') && 
            !token.startsWith('http') &&
            !token.includes(' ') &&
            token !== accountSid) {
            authToken = token;
            console.log("✓ Found Auth Token:", token.substring(0, 20) + "...");
            break;
        }
    }
    
    // Also check input fields
    if (!authToken) {
        const inputs = document.querySelectorAll('input[type="text"], input[type="password"], code, pre');
        for (const input of inputs) {
            const value = input.value || input.textContent || input.innerText || '';
            if (value.length >= 32 && 
                !value.startsWith('AC') && 
                !value.startsWith('http') &&
                value !== accountSid) {
                authToken = value.trim();
                console.log("✓ Found Auth Token in input:", authToken.substring(0, 20) + "...");
                break;
            }
        }
    }
    
    console.log("\n" + "=".repeat(60));
    console.log("Results:");
    console.log("=".repeat(60));
    
    if (accountSid) {
        console.log("Account SID:", accountSid);
    } else {
        console.log("⚠️  Account SID not found");
        console.log("   Look for it on the page (starts with AC...)");
    }
    
    if (authToken) {
        console.log("Auth Token:", authToken.substring(0, 20) + "...");
    } else {
        console.log("⚠️  Auth Token not found");
        console.log("   Make sure you clicked 'Show' next to Auth Token");
    }
    
    console.log("\n" + "=".repeat(60));
    console.log("Next Steps:");
    console.log("=".repeat(60));
    
    if (accountSid && authToken) {
        console.log("\nRun this command in your terminal:");
        console.log(`python scripts/debug_twilio_sms.py --account-sid ${accountSid} --auth-token ${authToken}`);
        
        // Also copy to clipboard if possible
        const command = `python scripts/debug_twilio_sms.py --account-sid ${accountSid} --auth-token ${authToken}`;
        if (navigator.clipboard) {
            navigator.clipboard.writeText(command).then(() => {
                console.log("\n✓ Command copied to clipboard!");
            });
        }
        
        // Create a download link for .env file
        const envContent = `TWILIO_ACCOUNT_SID=${accountSid}\nTWILIO_AUTH_TOKEN=${authToken}\n`;
        const blob = new Blob([envContent], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'twilio_creds.env';
        a.textContent = 'Download .env file';
        console.log("\nOr download .env file:");
        document.body.appendChild(a);
        console.log("(Check the download link that appeared on the page)");
    } else if (accountSid) {
        console.log("\nRun this command (add Auth Token manually):");
        console.log(`python scripts/debug_twilio_sms.py --account-sid ${accountSid} --auth-token YOUR_TOKEN`);
    } else {
        console.log("\nPlease manually copy:");
        console.log("1. Account SID from the page");
        console.log("2. Auth Token (click 'Show' to reveal)");
        console.log("\nThen run:");
        console.log("python scripts/debug_twilio_sms.py --account-sid AC... --auth-token YOUR_TOKEN");
    }
    
    return { accountSid, authToken };
})();







