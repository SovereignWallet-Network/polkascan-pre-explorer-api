import jwt
from app.settings import VALIDATOR_KEY, VALIDATOR_ISSUER 


def validateToken(token):
    try:
        if not token:
            print('No token')
            return False
        print(token)
        if not VALIDATOR_KEY:
            print('No Validator is configured, cant validate the token')
            return False
        token_data = False
        for validator in VALIDATOR_KEY:
            try:
                token_data = token_data = jwt.decode(token, validator, algorithms="HS256")
                break
            except jwt.InvalidSignatureError as e:
                print("Not signed by this validator")
                continue 
        # token_data = jwt.decode(token, VALIDATOR_KEY, algorithms="HS256")
        print(token_data)
        if token_data and 'iss' in token_data and token_data['iss'] in VALIDATOR_ISSUER:
            return token_data['data']
        print('Token issuer is invalid')
        return False
    except jwt.ExpiredSignatureError:
        print("Token expired")
        return False
